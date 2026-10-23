import base64
import os

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kbot import client as C
from kbot.auth import Signer
from kbot.config import ENVIRONMENTS, PRODUCTION_ARM_PHRASE, Settings


def test_ed25519_signature_verifies_and_excludes_query():
    k = ed25519.Ed25519PrivateKey.generate()
    h = Signer("kid", k).headers("get", "https://x/trade-api/v2/portfolio/orders?limit=5", ts_ms=1700000000000)
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    k.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                          b"1700000000000GET/trade-api/v2/portfolio/orders")


def test_rsa_pss_signature_verifies():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sig = Signer("kid", k).sign("1POST/trade-api/v2/portfolio/events/orders")
    k.public_key().verify(base64.b64decode(sig), b"1POST/trade-api/v2/portfolio/events/orders",
                          padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


def test_key_file_must_be_private(tmp_path):
    p = tmp_path / "k.pem"
    p.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        Signer.from_file("kid", str(p))
    os.chmod(p, 0o600)
    assert "kid" in repr(Signer.from_file("kid", str(p)))


def _signer():
    return Signer("kid", ed25519.Ed25519PrivateKey.generate())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(C.asyncio, "sleep", fast)


async def test_get_retries_on_429_then_succeeds():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429 if n["c"] < 3 else 200, json={"market": {"ticker": "T"}},
                              headers={"date": "Mon, 01 Jan 2024 00:00:00 GMT"})
    c = C.PublicClient("https://x/trade-api/v2", http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    assert (await c.market("T"))["ticker"] == "T"
    assert n["c"] == 3
    assert c.clock_skew_s is not None and c.clock_skew_s > 0   # local clock is after 2024


async def test_public_requests_are_unsigned():
    seen = {}

    def h(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"orderbook_fp": {}})
    c = C.PublicClient("https://x", http=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    await c.orderbook("T")
    assert "kalshi-access-key" not in seen


def _trading(handler, env="demo", armed=False, cancel_only=False):
    return C.TradingClient(ENVIRONMENTS[env]["rest"], _signer(), environment=env, armed=armed,
                           cancel_only=cancel_only,
                           http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_create_order_timeout_is_unknown_and_not_retried():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        raise httpx.ReadTimeout("slow", request=req)
    t = _trading(h)
    with pytest.raises(C.OrderStateUnknown):
        await t.create_order(ticker="T", book_side="bid", price=0.4, count=1, client_order_id="c1")
    assert n["c"] == 1


async def test_create_order_500_is_unknown_not_rejected():
    t = _trading(lambda r: httpx.Response(500, json={"error": "x"}))
    with pytest.raises(C.OrderStateUnknown):
        await t.create_order(ticker="T", book_side="bid", price=0.4, count=1, client_order_id="c1")


async def test_create_order_body_matches_v2_spec():
    body = {}

    def h(req):
        import json
        body.update(json.loads(req.content))
        assert req.url.path == "/trade-api/v2/portfolio/events/orders"
        assert req.headers["KALSHI-ACCESS-KEY"] == "kid"
        return httpx.Response(201, json={"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00",
                                         "ts_ms": 1})
    await _trading(h).create_order(ticker="T", book_side="ask", price=0.55, count=2, client_order_id="c1")
    assert body == {"ticker": "T", "side": "ask", "count": "2.00", "price": "0.5500",
                    "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
                    "client_order_id": "c1", "cancel_order_on_pause": True}


def test_production_client_refuses_unless_armed():
    with pytest.raises(C.ProductionDisabled):
        _trading(lambda r: httpx.Response(200), env="production", armed=False)


async def test_cancel_only_client_cannot_create():
    calls = []
    t = _trading(lambda r: calls.append(r) or httpx.Response(200, json={}), env="production", cancel_only=True)
    with pytest.raises(C.ProductionDisabled):
        await t.create_order(ticker="T", book_side="bid", price=0.4, count=1, client_order_id="c")
    assert calls == []


def test_make_trading_client_by_mode(tmp_path):
    s = Settings(mode="paper")
    with pytest.raises(C.ProductionDisabled):
        C.make_trading_client(s, _signer())
    s = Settings(mode="production", data_env="production")
    with pytest.raises(C.ProductionDisabled):
        C.make_trading_client(s, _signer())
    s.production_enabled = True
    with pytest.raises(C.ProductionDisabled):   # enabled but wrong arm phrase
        C.make_trading_client(s, _signer())
    s.production_arm = PRODUCTION_ARM_PHRASE
    assert C.make_trading_client(s, _signer()).environment == "production"
    d = C.make_trading_client(Settings(mode="demo", data_env="demo"), _signer())
    assert d.environment == "demo" and "demo" in d.base


async def test_rate_limiter_spaces_requests(monkeypatch):
    slept = []

    async def rec(x):
        slept.append(x)
    monkeypatch.setattr(C.asyncio, "sleep", rec)
    b = C.TokenBucket(20)       # two 10-token requests per second
    for _ in range(3):
        await b.take(10)
    assert slept and slept[0] > 0

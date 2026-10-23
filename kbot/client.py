"""Kalshi REST client, split by permission level.

PublicClient   market data, no credentials.
AccountClient  authenticated reads (balance, positions, orders, fills, settlements).
TradingClient  order create/cancel. Built only through `make_trading_client`, which
               refuses production unless it is explicitly armed.

Nothing here can transfer funds, withdraw, or change account settings: those endpoints
are simply not implemented.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from typing import Any

import httpx

from .auth import Signer
from .config import ENVIRONMENTS, Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiError(Exception):
    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"{status} on {path}: {body}")
        self.status, self.body, self.path = status, body, path


class OrderStateUnknown(Exception):
    """The create request may or may not have reached the exchange. Reconcile before retrying."""


class ProductionDisabled(Exception):
    pass


class TokenBucket:
    """Kalshi budgets tokens per second; most requests cost 10, cancels cost 2."""

    def __init__(self, per_second: float):
        self.rate = self.capacity = per_second
        self.tokens = per_second
        self.t = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, cost: float) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                await asyncio.sleep((cost - self.tokens) / self.rate)


class _Base:
    def __init__(self, base_url: str, signer: Signer | None = None, *,
                 read_tokens_per_s: float = 200, write_tokens_per_s: float = 100,
                 http: httpx.AsyncClient | None = None, timeout: float = 10.0,
                 max_retries: int = 4):
        self.base = base_url.rstrip("/")
        self.signer = signer
        self.http = http or httpx.AsyncClient(timeout=timeout)
        self.read_bucket = TokenBucket(read_tokens_per_s)
        self.write_bucket = TokenBucket(write_tokens_per_s)
        self.max_retries = max_retries
        self.clock_skew_s: float | None = None   # local - server
        self.last_ok: float | None = None

    async def close(self):
        await self.http.aclose()

    async def _request(self, method: str, path: str, *, params=None, json=None,
                       auth=False, cost=10, write=False, retry=True) -> Any:
        url = self.base + path
        bucket = self.write_bucket if write else self.read_bucket
        attempt = 0
        while True:
            await bucket.take(cost)
            headers = self.signer.headers(method, url) if auth else {}
            try:
                r = await self.http.request(method, url, params=params, json=json, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if not retry:
                    raise OrderStateUnknown(f"{method} {path}: {e!r}") from e
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 16))
                continue
            self._track_clock(r)
            if r.status_code in RETRYABLE_STATUS and retry and attempt < self.max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 16))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text[:500]
                if not retry and r.status_code >= 500:
                    # A 5xx on create can still mean the order was accepted.
                    raise OrderStateUnknown(f"{method} {path}: HTTP {r.status_code} {body}")
                raise KalshiError(r.status_code, body, path)
            self.last_ok = time.time()
            return r.json() if r.content else {}

    def _track_clock(self, r: httpx.Response) -> None:
        date = r.headers.get("date")
        if date:
            try:
                server = email.utils.parsedate_to_datetime(date).timestamp()
                self.clock_skew_s = time.time() - server
            except (TypeError, ValueError):
                pass

    async def paginate(self, path: str, key: str, params: dict | None = None,
                       auth: bool = False, max_pages: int = 1000):
        params = dict(params or {})
        params.setdefault("limit", 1000)
        for _ in range(max_pages):
            data = await self._request("GET", path, params=params, auth=auth)
            for item in data.get(key) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor


class PublicClient(_Base):
    async def exchange_status(self):
        return await self._request("GET", "/exchange/status")

    async def series_list(self, **params):
        return (await self._request("GET", "/series", params=params)).get("series") or []

    async def series(self, ticker: str):
        return (await self._request("GET", f"/series/{ticker}")).get("series")

    async def fee_changes(self, series_ticker: str | None = None):
        params = {"series_ticker": series_ticker} if series_ticker else None
        return (await self._request("GET", "/series/fee_changes", params=params)).get(
            "series_fee_change_arr") or []

    def markets(self, **params):
        return self.paginate("/markets", "markets", params)

    async def market(self, ticker: str):
        return (await self._request("GET", f"/markets/{ticker}")).get("market")

    async def orderbook(self, ticker: str, depth: int = 0):
        params = {"depth": depth} if depth else None
        return (await self._request("GET", f"/markets/{ticker}/orderbook", params=params)).get(
            "orderbook_fp") or {}

    async def historical_candlesticks(self, ticker: str, start_ts: int, end_ts: int, period: int = 1):
        """One market. Needed for markets older than the batch endpoint serves (about 2 months).
        Prices here come as `close`, not `close_dollars`."""
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period}
        return (await self._request("GET", f"/historical/markets/{ticker}/candlesticks",
                                    params=params)).get("candlesticks") or []

    async def batch_candlesticks(self, tickers: list[str], start_ts: int, end_ts: int,
                                 period: int = 1) -> dict[str, list]:
        """Up to 100 tickers and 10,000 candles per call. Covers live and historical markets."""
        params = {"market_tickers": ",".join(tickers), "start_ts": start_ts, "end_ts": end_ts,
                  "period_interval": period}
        data = await self._request("GET", "/markets/candlesticks", params=params)
        return {m["market_ticker"]: m.get("candlesticks") or [] for m in data.get("markets") or []}

    async def historical_cutoff(self):
        return await self._request("GET", "/historical/cutoff")

    def historical_markets(self, **params):
        return self.paginate("/historical/markets", "markets", params)

    def trades(self, historical: bool = False, **params):
        return self.paginate("/historical/trades" if historical else "/markets/trades",
                             "trades", params)


class AccountClient(PublicClient):
    """Authenticated reads. Requires a signer."""

    def __init__(self, base_url: str, signer: Signer, **kw):
        if signer is None:
            raise ValueError("AccountClient needs credentials")
        super().__init__(base_url, signer, **kw)

    async def balance(self):
        return await self._request("GET", "/portfolio/balance", auth=True)

    def positions(self, **params):
        return self.paginate("/portfolio/positions", "market_positions", params, auth=True)

    def orders(self, **params):
        return self.paginate("/portfolio/orders", "orders", params, auth=True)

    async def order(self, order_id: str):
        return (await self._request("GET", f"/portfolio/orders/{order_id}", auth=True)).get("order")

    def fills(self, **params):
        return self.paginate("/portfolio/fills", "fills", params, auth=True)

    def settlements(self, **params):
        return self.paginate("/portfolio/settlements", "settlements", params, auth=True)

    async def find_order_by_client_id(self, ticker: str, client_order_id: str):
        """No server-side filter exists for client_order_id, so scan this market's orders."""
        async for o in self.orders(ticker=ticker):
            if o.get("client_order_id") == client_order_id:
                return o
        return None


class TradingClient(AccountClient):
    def __init__(self, base_url: str, signer: Signer, *, environment: str, armed: bool,
                 cancel_only: bool = False, **kw):
        if environment == "production" and not armed and not cancel_only:
            raise ProductionDisabled("production trading is not armed")
        if base_url.rstrip("/") != ENVIRONMENTS[environment]["rest"]:
            raise ValueError("trading base URL does not match its declared environment")
        super().__init__(base_url, signer, **kw)
        self.environment = environment
        self.armed = armed and not cancel_only
        self.cancel_only = cancel_only

    async def create_order(self, *, ticker: str, book_side: str, price: float, count: float,
                           client_order_id: str, time_in_force: str = "immediate_or_cancel",
                           post_only: bool = False, reduce_only: bool = False,
                           expiration_time: int | None = None):
        if self.cancel_only or (self.environment == "production" and not self.armed):
            raise ProductionDisabled("this client cannot create orders")
        if book_side not in ("bid", "ask"):
            raise ValueError("book_side must be bid or ask")
        if not 0 < price < 1:
            raise ValueError("price out of range")
        body = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
            "cancel_order_on_pause": True,
        }
        if post_only:
            body["post_only"] = True
        if reduce_only:
            body["reduce_only"] = True
        if expiration_time:
            body["expiration_time"] = expiration_time
        # retry=False: never resend a create after a timeout; caller reconciles instead.
        return await self._request("POST", "/portfolio/events/orders", json=body, auth=True,
                                   write=True, retry=False)

    async def cancel_order(self, order_id: str, ticker: str):
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                                   params={"market_ticker": ticker}, auth=True, write=True, cost=2)


def make_public_client(s: Settings, **kw) -> PublicClient:
    return PublicClient(ENVIRONMENTS[s.data_env]["rest"], **kw)


def make_signer(s: Settings) -> Signer:
    if not s.api_key_id or not s.private_key_path:
        env = "PROD" if s.mode == "production" else "DEMO"
        raise ValueError(f"set KBOT_{env}_KEY_ID and KBOT_{env}_PRIVATE_KEY_PATH")
    return Signer.from_file(s.api_key_id, s.private_key_path)


def make_trading_client(s: Settings, signer: Signer | None = None, cancel_only: bool = False,
                        **kw) -> TradingClient:
    if s.mode not in ("demo", "production"):
        raise ProductionDisabled(f"mode {s.mode} never sends orders")
    env = s.trade_env
    return TradingClient(ENVIRONMENTS[env]["rest"], signer or make_signer(s), environment=env,
                         armed=s.production_armed(), cancel_only=cancel_only, **kw)

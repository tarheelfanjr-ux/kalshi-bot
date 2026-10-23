"""Maker bids as real orders, against an in-memory demo exchange with the API's request and response
shapes. Nothing here reaches the network. The App still refuses the maker strategy outside paper
mode; these tests build the engine directly to check the order path that lifting that guard would use."""
import json
import time

import httpx
import pytest

from kbot import client as C
from kbot.config import RiskLimits
from kbot.discovery import Discovery
from kbot.execution import KalshiAdapter, OrderManager
from kbot.maker import MakerEngine, report
from kbot.risk import RiskManager

from conftest import api_market, book
from test_auth_client import _trading
from test_maker import MakerPublic

T = "KXBTC15M-M"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(C.asyncio, "sleep", fast)


class FakeExchange:
    """Order endpoints of the demo exchange, in memory."""

    def __init__(self):
        self.orders, self.posts, self.cancels = {}, [], []

    def __call__(self, req):
        path = req.url.path.removeprefix("/trade-api/v2")
        if req.method == "POST" and path == "/portfolio/events/orders":
            b = json.loads(req.content)
            self.posts.append(b)
            oid = f"o{len(self.orders) + 1}"
            self.orders[oid] = {"order_id": oid, "client_order_id": b["client_order_id"], "ticker": b["ticker"],
                                "outcome_side": "yes" if b["side"] == "bid" else "no", "book_side": b["side"],
                                "status": "resting", "yes_price_dollars": b["price"], "fill_count_fp": "0.00",
                                "remaining_count_fp": b["count"], "taker_fill_cost_dollars": "0.000000",
                                "maker_fill_cost_dollars": "0.000000", "taker_fees_dollars": "0.000000",
                                "maker_fees_dollars": "0.000000"}
            return httpx.Response(201, json={"order_id": oid, "client_order_id": b["client_order_id"],
                                             "fill_count": "0.00", "remaining_count": b["count"], "ts_ms": 1})
        if req.method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            self.cancels.append(oid)
            o = self.orders.get(oid)
            if not o or o["status"] != "resting":
                return httpx.Response(404, json={"code": "not_found"})
            o.update(status="canceled", remaining_count_fp="0.00")
            return httpx.Response(200, json={"order": o})
        if req.method == "GET" and path.startswith("/portfolio/orders/"):
            return httpx.Response(200, json={"order": self.orders[path.rsplit("/", 1)[1]]})
        if req.method == "GET" and path == "/portfolio/orders":
            t = req.url.params.get("ticker")
            return httpx.Response(200, json={"orders": [o for o in self.orders.values() if o["ticker"] == t]})
        if req.method == "GET" and path == "/portfolio/positions":
            pos = {}
            for o in self.orders.values():
                n = float(o["fill_count_fp"]) * (1 if o["outcome_side"] == "yes" else -1)
                pos[o["ticker"]] = pos.get(o["ticker"], 0) + n
            return httpx.Response(200, json={"market_positions": [{"ticker": t, "position_fp": f"{n:.2f}"}
                                                                  for t, n in pos.items() if n]})
        return httpx.Response(404, json={})

    def fill(self, oid):
        """A taker trades into our resting bid: all of it, at our price, as maker, no maker fee."""
        o = self.orders[oid]
        yes = float(o["yes_price_dollars"])
        o.update(status="executed", fill_count_fp=o["remaining_count_fp"], remaining_count_fp="0.00",
                 maker_fill_cost_dollars=f"{yes if o['outcome_side'] == 'yes' else 1 - yes:.6f}")


async def live_engine(db, bankroll=25.0):
    now = time.time() // 60 * 60 + 1       # one second into a minute
    pub = MakerPublic({T: api_market(T, now, close_in=600)}, {})
    disc = Discovery(pub, db, ["KXBTC15M"])
    await disc.refresh()
    ex = FakeExchange()
    adapter = KalshiAdapter(_trading(ex))
    om = OrderManager(db, adapter)
    risk = RiskManager(db, RiskLimits(bankroll=bankroll), "demo")
    books = {}
    eng = MakerEngine(db, disc, om, risk, books, adapter)
    return eng, ex, pub, disc.markets[T], books, now


async def test_live_bids_are_post_only_gtc_and_cancelled_when_the_quote_ends(db):
    eng, ex, pub, m, books, now = await live_engine(db, bankroll=100.0)     # $1 per position: both sides fit
    assert not eng.paper and eng.mode == "demo"
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, True, "ok")
    common = {"ticker": T, "count": "1.00", "time_in_force": "good_till_canceled", "post_only": True,
              "expiration_time": int(m.close_ts), "self_trade_prevention_type": "taker_at_cross",
              "cancel_order_on_pause": True}
    assert [{k: v for k, v in p.items() if k != "client_order_id"} for p in ex.posts] == [
        {**common, "side": "bid", "price": "0.2000"},          # YES bid at 0.20
        {**common, "side": "ask", "price": "0.2400"}]          # NO bid at 0.76 is a YES ask at 0.24
    assert [o["state"] for o in db.q("SELECT state FROM orders ORDER BY created_at")] == ["resting", "resting"]
    # The tape says YES was hit. That fills the research log, never the real order.
    eng.on_trade(T, now + 4, "no", 0.19)
    yes = db.one("SELECT * FROM quotes WHERE side='yes'")
    assert yes["status"] == "filled" and yes["end_ts"] == now + 64
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (yes["client_order_id"],))["state"] == "resting"
    assert not db.q("SELECT * FROM positions")
    await eng.cancel_ended(now + 30)
    assert ex.cancels == []                     # the quote is still live, so is its order
    books[T] = book(T, 0.20, 0.24, ts=now + 60)
    await eng.quote(m, now + 60, True, "ok")     # YES filled on the tape: not requoted. NO unchanged: extended
    await eng.cancel_ended(now + 65)             # the YES quote ended: its order goes at once, NO keeps resting
    assert ex.cancels == ["o1"] and ex.orders["o1"]["status"] == "canceled" and ex.orders["o2"]["status"] == "resting"
    assert len(ex.posts) == 2
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (yes["client_order_id"],))["state"] == "canceled"
    eng._polled[T] = now + 20
    await eng.expire()
    assert report(db, "demo")["fill_model_check"]["tape_only"] == 1


async def test_live_fill_comes_from_the_exchange_and_blocks_a_second_bid(db):
    eng, ex, pub, m, books, now = await live_engine(db)
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, True, "ok")          # $25 bankroll: only the 0.20 YES bid fits the $0.25 budget
    assert len(ex.posts) == 1
    ex.fill("o1")
    await eng.om.reconcile_orders()
    f = db.one("SELECT * FROM fills")
    assert (f["price"], f["fee"], f["is_taker"], f["count"]) == (pytest.approx(0.20), 0.0, 0, 1.0)
    p = db.one("SELECT * FROM positions")
    assert (p["outcome_side"], p["count"], p["cost"]) == ("yes", 1.0, pytest.approx(0.20))
    books[T] = book(T, 0.20, 0.24, ts=now + 60)
    await eng.quote(m, now + 60, True, "ok")     # same price, order filled: inventory is full
    assert len(ex.posts) == 1 and "inventory" in db.one("SELECT risk FROM quotes WHERE side='yes'")["risk"]
    assert await eng.om.reconcile_positions() == []


async def test_requote_cancels_the_old_bid_first_and_counts_a_fill_that_beat_the_cancel(db):
    eng, ex, pub, m, books, now = await live_engine(db)
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, True, "ok")
    ex.fill("o1")                                # filled on the exchange; we have not looked yet
    books[T] = book(T, 0.21, 0.24, ts=now + 60)
    await eng.quote(m, now + 60, True, "ok")     # new price: cancel o1 (too late), read it back, no new bid
    assert ex.cancels == ["o1"] and len(ex.posts) == 1
    assert db.one("SELECT count FROM positions WHERE outcome_side='yes'")["count"] == 1.0
    assert "inventory" in db.one("SELECT risk FROM quotes WHERE price=0.21")["risk"]


async def test_requote_moves_an_unfilled_bid(db):
    eng, ex, pub, m, books, now = await live_engine(db)
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, True, "ok")
    books[T] = book(T, 0.21, 0.24, ts=now + 60)
    await eng.quote(m, now + 60, True, "ok")
    assert ex.cancels == ["o1"] and [p["price"] for p in ex.posts] == ["0.2000", "0.2100"]
    assert [o["status"] for o in ex.orders.values()] == ["canceled", "resting"]


async def test_halt_and_estop_cancel_resting_bids_and_block_new_ones(db):
    eng, ex, pub, m, books, now = await live_engine(db)
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, True, "ok")
    eng.risk.trip("daily loss limit")
    assert await eng.om.cancel_open_entries("daily loss limit") == 1
    assert ex.orders["o1"]["status"] == "canceled"
    books[T] = book(T, 0.19, 0.24, ts=now + 60)
    await eng.quote(m, now + 60, True, "ok")
    assert len(ex.posts) == 1 and "halted" in db.one("SELECT risk FROM quotes WHERE price=0.19")["risk"]
    db.set(f"halt:{eng.mode}", None)
    db.set("estop", {"reason": "test"})
    books[T] = book(T, 0.18, 0.24, ts=now + 120)
    await eng.quote(m, now + 120, True, "ok")
    assert len(ex.posts) == 1 and "estop" in db.one("SELECT risk FROM quotes WHERE price=0.18")["risk"]


async def test_unhealthy_feed_places_no_real_bid(db):
    eng, ex, pub, m, books, now = await live_engine(db)
    books[T] = book(T, 0.20, 0.24, ts=now)
    await eng.quote(m, now, False, "reconciliation older than 120 s")
    assert ex.posts == [] and "unhealthy" in db.one("SELECT risk FROM quotes WHERE side='yes'")["risk"]


async def test_production_engine_cannot_exist_unarmed(db):
    with pytest.raises(C.ProductionDisabled):
        KalshiAdapter(_trading(FakeExchange(), env="production", armed=False))


async def test_paper_shutdown_cancels_resting_maker_bids(tmp_path):
    from test_maker import maker_app
    app, pub, now = maker_app(tmp_path)
    app.startup_checks()
    await app.tick()
    assert [o["state"] for o in app.db.q("SELECT state FROM orders")] == ["resting"]
    await app.shutdown([])
    assert [o["state"] for o in app.db.q("SELECT state FROM orders")] == ["canceled"]

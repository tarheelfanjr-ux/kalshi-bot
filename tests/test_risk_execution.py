import time

import pytest

from kbot.client import KalshiError, OrderStateUnknown
from kbot.config import RiskLimits
from kbot.db import DB
from kbot.execution import ExchangeOrder, IllegalTransition, OrderManager, PaperAdapter
from kbot.risk import RiskManager
from kbot.strategy import Candidate

from conftest import book


def cand(ticker="T1", price=0.40, count=100, side="yes"):
    return Candidate(ticker, time.time() + 600, side, price, price, 0.6, 0.41, 0.2, 0.02, 0.15, 0.02, 100,
                     count, 5, "test", "test", [])


class FakeAdapter:
    """Scripted exchange. `script` is a list of ExchangeOrder results or exceptions."""
    mode = "demo"

    def __init__(self, script=(), remote_positions=None):
        self.script = list(script)
        self.submits = []
        self.known = {}
        self.remote = remote_positions

    async def submit(self, **kw):
        self.submits.append(kw)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def cancel(self, ticker, oid):
        self.known_cancel = (ticker, oid)

    async def lookup(self, ticker, coid, oid):
        return self.known.get(coid)

    async def positions(self):
        return self.remote


# --- risk ----------------------------------------------------------------------
def test_position_size_limited_by_worst_case_loss(db):
    r = RiskManager(db, RiskLimits(bankroll=1000), "demo")
    d = r.approve(cand(price=0.40))
    # 1% of 1000 = $10. 23 * 0.40 = 9.20 + fee 0.39 = 9.59 fits; 24 would be 9.60 + 0.41 = 10.01.
    assert d.approved and d.count == 23


def test_tiny_bankroll_skips_trade(db):
    d = RiskManager(db, RiskLimits(bankroll=25), "demo").approve(cand(price=0.40))
    assert not d.approved and "too small" in d.reason


def test_aggregate_risk_includes_resting_and_partial_orders(db):
    r = RiskManager(db, RiskLimits(bankroll=1000, max_open_positions=10), "demo")
    for i in range(5):  # resting entries: 4 x (24 @ 0.40 + fee 0.41) + 1 x (14 left @ 0.40 + fee 0.24)
        db.x("INSERT INTO orders(client_order_id,mode,ticker,outcome_side,limit_price,count,filled,state) "
             "VALUES(?,?,?,?,?,?,?,?)", (f"c{i}", "demo", f"X{i}", "yes", 0.40, 24, 10 if i == 0 else 0,
                                         "partially_filled" if i == 0 else "resting"))
    assert r.open_risk() == pytest.approx(4 * 10.01 + 5.84)
    d = r.approve(cand("NEW"))      # $4.12 headroom left -> 9 contracts (3.60 + 0.16 fee)
    assert d.approved and d.count == 9


def test_no_averaging_into_same_market_and_max_positions(db):
    r = RiskManager(db, RiskLimits(bankroll=1000, max_open_positions=2), "demo")
    db.x("INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,status) VALUES('demo','T1','yes',1,0.4,0.02,'open')")
    assert "averaging" in r.approve(cand("T1")).reason
    db.x("INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,status) VALUES('demo','T2','yes',1,0.4,0.02,'open')")
    assert "open positions" in r.approve(cand("T3")).reason


def test_daily_loss_limit_survives_restart(tmp_path):
    path = str(tmp_path / "r.sqlite")
    db = DB(path)
    db.ledger("demo", "settlement", -25.0, "T0")
    db.close()
    db2 = DB(path)                                           # "restart"
    r = RiskManager(db2, RiskLimits(bankroll=1000), "demo")  # limit 2% = $20
    d = r.approve(cand())
    assert not d.approved and d.trip and "daily realized loss" in d.reason
    assert r.halted()                                          # persisted halt
    assert RiskManager(DB(path), RiskLimits(bankroll=1000), "demo").halted()


def test_daily_loss_resets_only_on_new_utc_day(db):
    clock = [1_700_000_000.0]
    r = RiskManager(db, RiskLimits(bankroll=1000), "demo", clock=lambda: clock[0])
    db.ledger("demo", "settlement", -25.0, "T", ts=clock[0])
    assert r.check_limits()
    clock[0] += 86400
    assert r.check_limits() is None


def test_consecutive_losses_require_review(db):
    r = RiskManager(db, RiskLimits(bankroll=100000), "demo")
    for i in range(5):
        db.x("INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,status,pnl,settled_at) "
             "VALUES('demo',?,'yes',1,0.4,0.02,'settled',-0.42,?)", (f"L{i}", time.time() - 100 + i))
    d = r.approve(cand())
    assert not d.approved and "consecutive" in d.reason
    r.clear("reviewed by test")
    assert r.approve(cand()).approved          # streak counts from the review onward


def test_pause_estop_and_unhealthy_block_entries(db):
    r = RiskManager(db, RiskLimits(bankroll=1000), "demo")
    assert not r.approve(cand(), healthy=False, health_reason="stale").approved
    db.set("pause", {"reason": "x"})
    assert "pause" in r.approve(cand()).reason
    db.set("pause", None)
    db.set("estop", {"reason": "x"})
    assert "estop" in r.approve(cand()).reason


# --- execution -------------------------------------------------------------------
async def test_fill_records_position_and_fees(db):
    a = FakeAdapter([ExchangeOrder("o1", "executed", 5, 0, 0.40, 0.09)])
    om = OrderManager(db, a)
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=5)
    o = db.one("SELECT * FROM orders WHERE client_order_id=?", (coid,))
    assert o["state"] == "filled" and o["filled"] == 5
    p = db.one("SELECT * FROM positions WHERE ticker='T1'")
    assert p["count"] == 5 and p["cost"] == pytest.approx(2.0) and p["fees"] == pytest.approx(0.09)


async def test_partial_fill_then_cancel(db):
    om = OrderManager(db, FakeAdapter([ExchangeOrder("o1", "canceled", 2, 0, 0.40, 0.04)]))
    coid = await om.place(ticker="T1", outcome_side="no", limit_price=0.42, count=5)
    o = db.one("SELECT * FROM orders WHERE client_order_id=?", (coid,))
    assert o["state"] == "canceled" and o["filled"] == 2
    assert db.one("SELECT count FROM positions WHERE ticker='T1' AND outcome_side='no'")["count"] == 2


async def test_timeout_never_resubmits_and_reconcile_recovers(db):
    a = FakeAdapter([OrderStateUnknown("timeout")])
    om = OrderManager(db, a)
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=3)
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "unknown"
    # While unknown, the risk manager treats the market as busy: no duplicate order.
    assert "averaging" in RiskManager(db, RiskLimits(bankroll=1000), "demo").approve(cand("T1")).reason
    a.known[coid] = ExchangeOrder("o9", "executed", 3, 0, 0.41, 0.06)
    assert await om.reconcile_orders() == []
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "filled"
    assert len(a.submits) == 1
    assert db.one("SELECT count FROM positions WHERE ticker='T1'")["count"] == 3


async def test_unknown_order_not_on_exchange_becomes_rejected(db):
    om = OrderManager(db, FakeAdapter([OrderStateUnknown("timeout")]))
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=3)
    db.x("UPDATE orders SET created_at=? WHERE client_order_id=?", (time.time() - 120, coid))
    await om.reconcile_orders()
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "rejected"


async def test_insufficient_funds_rejects_and_halts(db):
    om = OrderManager(db, FakeAdapter([KalshiError(400, {"code": "insufficient_balance"}, "/x")]))
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=3)
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "rejected"
    assert db.get("halt:demo")


async def test_illegal_transition_raises(db):
    om = OrderManager(db, FakeAdapter([ExchangeOrder("o1", "executed", 1, 0, 0.4, 0.01)]))
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=1)
    with pytest.raises(IllegalTransition):
        om._set_state(coid, "resting")


async def test_settlement_accounting_yes_and_no(db):
    om = OrderManager(db, FakeAdapter([ExchangeOrder("a", "executed", 10, 0, 0.30, 0.15),
                                       ExchangeOrder("b", "executed", 10, 0, 0.60, 0.17)]))
    await om.place(ticker="A", outcome_side="yes", limit_price=0.3, count=10)
    await om.place(ticker="B", outcome_side="no", limit_price=0.6, count=10)
    assert om.settle("A", "yes") == pytest.approx(10 - 3.0 - 0.15)
    assert om.settle("B", "yes") == pytest.approx(-6.0 - 0.17)
    assert om.settle("B", "yes") == 0       # idempotent: already settled
    assert db.daily_realized("demo") == pytest.approx(6.85 - 6.17)


async def test_position_mismatch_is_reported(db):
    om = OrderManager(db, FakeAdapter([ExchangeOrder("a", "executed", 2, 0, 0.3, 0.03)], remote_positions={"A": 1.0}))
    await om.place(ticker="A", outcome_side="yes", limit_price=0.3, count=2)
    assert await om.reconcile_positions() == ["A: local 2.0 vs exchange 1.0"]


async def test_cancel_open_entries(db):
    a = FakeAdapter([ExchangeOrder("o1", "resting", 0, 4, None, 0)])
    om = OrderManager(db, a)
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=4)
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "resting"
    a.known[coid] = ExchangeOrder("o1", "canceled", 0, 0, None, 0)
    assert await om.cancel_open_entries("test") == 1
    assert db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"] == "canceled"


async def test_paper_fill_model_is_partial_and_rejects_stale_books():
    b = book(yes_bid=0.45, yes_ask=0.47, size=10)
    pa = PaperAdapter(lambda t: b, fill_fraction=0.5)
    eo = await pa.submit(ticker="T", outcome_side="yes", limit_price=0.50, count=8, client_order_id="c" * 10)
    assert eo.filled == 5 and eo.avg_price == 0.47 and eo.fees > 0
    b.updated_at -= 60
    eo = await pa.submit(ticker="T", outcome_side="yes", limit_price=0.50, count=8, client_order_id="d" * 10)
    assert eo.status == "rejected"


async def test_ioc_partial_with_zero_remaining_is_canceled_not_filled(db):
    om = OrderManager(db, FakeAdapter([ExchangeOrder("o1", "executed", 2, 0, 0.40, 0.04)]))
    coid = await om.place(ticker="T1", outcome_side="yes", limit_price=0.42, count=5)
    o = db.one("SELECT * FROM orders WHERE client_order_id=?", (coid,))
    assert o["state"] == "canceled" and o["filled"] == 2


async def test_closed_market_awaiting_settlement_is_not_a_mismatch(db):
    db.x("INSERT INTO markets(ticker,close_ts) VALUES('A',?)", (time.time() - 10,))
    om = OrderManager(db, FakeAdapter([ExchangeOrder("a", "executed", 2, 0, 0.3, 0.03)], remote_positions={}))
    await om.place(ticker="A", outcome_side="yes", limit_price=0.3, count=2)
    assert await om.reconcile_positions() == []


def test_kalshi_adapter_maps_no_side_to_yes_leg_ask():
    from kbot.execution import KalshiAdapter
    assert KalshiAdapter._to_book("yes", 0.42) == ("bid", 0.42)
    assert KalshiAdapter._to_book("no", 0.42) == ("ask", 0.58)
    eo = KalshiAdapter._from_api({"order_id": "x", "fill_count": "3.00", "remaining_count": "0.00",
                                  "average_fill_price": "0.5800", "average_fee_paid": "0.0200"}, "no")
    assert eo.filled == 3 and eo.avg_price == pytest.approx(0.42) and eo.fees == pytest.approx(0.06)

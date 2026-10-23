"""Maker strategy: quotes, fills only from trades that print through our price, limits, report."""
import time
from datetime import datetime, timezone

import pytest

from kbot.app import App
from kbot.book import OrderBook
from kbot.config import RiskLimits, Settings, StrategyConfig
from kbot.db import DB
from kbot.execution import PaperAdapter
from kbot.maker import HOLDOUT_START, SHADOW_REASON, history, report
from kbot.risk import RiskManager

from conftest import FakePublic, api_market, book
from test_app_backtest import FakeSpot


def iso_us(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class MakerPublic(FakePublic):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.tape = []

    def trade(self, ticker, t, taker_side, yes_price):
        self.tape.append({"trade_id": f"{ticker}-{len(self.tape)}", "ticker": ticker, "created_time": iso_us(t),
                          "taker_side": taker_side, "yes_price_dollars": f"{yes_price:.4f}", "count_fp": "3.00"})

    async def trades(self, historical=False, **params):
        self.calls.append(("trades", params["ticker"]))
        for x in self.tape:
            if x["ticker"] == params["ticker"]:
                yield x


def maker_app(tmp_path, yes_bid=0.20, yes_ask=0.24):
    now = time.time()
    s = Settings(mode="paper", db_path=str(tmp_path / "m.sqlite"), series=["KXBTC15M"])
    s.risk, s.strategy = RiskLimits(), StrategyConfig(name="maker")      # the real $25 limits
    pub = MakerPublic({"KXBTC15M-M": api_market("KXBTC15M-M", now, close_in=600, strike=100.0)},
                      {"KXBTC15M-M": {"yes_dollars": [[f"{yes_bid:.2f}", "50.00"]],
                                      "no_dollars": [[f"{1 - yes_ask:.2f}", "40.00"]]}})
    app = App(s)
    app.public = app.discovery.client = app.poller.client = pub
    app.coinbase = FakeSpot(app.spot, 100.2)
    return app, pub, now


async def test_maker_quotes_fill_only_on_trade_through_and_settle(tmp_path):
    app, pub, now = maker_app(tmp_path)
    app.startup_checks()
    await app.tick()
    q = {r["side"]: r for r in app.db.q("SELECT * FROM quotes")}
    assert (q["yes"]["price"], q["no"]["price"], q["yes"]["queue"]) == (0.20, 0.76, 50.0)
    # $25 bankroll, 1% per position: the 0.20 bid fits, the 0.76 bid does not. Both are still logged.
    assert q["yes"]["client_order_id"] and not q["no"]["client_order_id"] and "budget" in q["no"]["risk"]
    assert [r["state"] for r in app.db.q("SELECT state FROM orders")] == ["resting"]
    t0 = q["yes"]["quote_ts"]
    pub.trade("KXBTC15M-M", t0 + 0.5, "no", 0.19)     # before our 1 s latency: not ours
    pub.trade("KXBTC15M-M", t0 + 2, "no", 0.20)       # at our price: queue unknown, no fill
    pub.trade("KXBTC15M-M", t0 + 3, "yes", 0.18)      # a taker buying YES never hits our YES bid
    await app.maker.poll_trades()
    assert app.db.one("SELECT status FROM quotes WHERE side='yes'")["status"] == "open"
    pub.trade("KXBTC15M-M", t0 + 4, "no", 0.19)       # prints through 0.20: we were hit
    pub.trade("KXBTC15M-M", t0 + 5, "yes", 0.26)      # NO side at 0.74, through our 0.76 NO bid
    pub.trade("KXBTC15M-M", t0 + 6, "no", 0.17)       # second fill on YES side: not allowed
    await app.maker.poll_trades()
    await app.om.reconcile_orders()
    q = {r["side"]: r for r in app.db.q("SELECT * FROM quotes")}
    assert (q["yes"]["status"], q["yes"]["fill_ts"], q["no"]["status"]) == ("filled", pytest.approx(t0 + 4), "filled")
    pos = app.db.q("SELECT * FROM positions")
    assert [(p["outcome_side"], p["count"], p["cost"], p["fees"]) for p in pos] == [("yes", 1.0, 0.20, 0.0)]
    assert app.db.one("SELECT is_taker FROM fills")["is_taker"] == 0
    # No taker order was ever sent; fair_value was logged once per market as a shadow.
    await app.tick()
    assert all(o["state"] in ("filled", "resting", "canceled") for o in app.db.q("SELECT state FROM orders"))
    assert len(app.db.q("SELECT * FROM signals WHERE reason=?", (SHADOW_REASON,))) == 1
    # Settlement: YES wins. The paper account books +0.80; the log has both fills.
    app.db.x("UPDATE positions SET close_ts=?", (time.time() - 60,))
    pub.results["KXBTC15M-M"] = "yes"
    await app.settle_finished()
    assert app.db.one("SELECT pnl FROM positions")["pnl"] == pytest.approx(0.80)
    rep = report(app.db, "paper", 25.0)
    log = rep["maker_research_log"]
    assert (log["fills"], log["settled_fills"]) == (2, 2)
    assert log["gross_per_contract"]["mean"] == pytest.approx((0.80 - 0.76) / 2)
    assert rep["paper_account"]["net_pnl"] == pytest.approx(0.80) and rep["no_trade_baseline"]["net_pnl"] == 0
    assert rep["fair_value_shadow"]["settled"] == 1 and "SHORT PROSPECTIVE" in rep["label"]


async def test_requote_ends_old_quote_and_cancels_its_order_after_the_tape_is_read(tmp_path):
    app, pub, now = maker_app(tmp_path)
    await app.discovery.refresh()
    m = app.discovery.markets["KXBTC15M-M"]
    app.books[m.ticker] = book(m.ticker, 0.20, 0.24, ts=now)
    await app.maker.quote(m, now, True, "ok")
    app.books[m.ticker] = book(m.ticker, 0.20, 0.24, ts=now + 30)
    await app.maker.quote(m, now + 30, True, "ok")      # same price: same quote, keeps its place
    assert len(app.db.q("SELECT * FROM quotes WHERE side='yes'")) == 1
    app.books[m.ticker] = book(m.ticker, 0.21, 0.24, ts=now + 60)
    await app.maker.quote(m, now + 60, True, "ok")
    old, new = app.db.q("SELECT * FROM quotes WHERE side='yes' ORDER BY id")
    assert old["end_ts"] == now + 60 and new["price"] == 0.21
    app.maker._polled[m.ticker] = now + 62            # tape not read far enough past the end yet
    await app.maker.expire()
    assert app.db.one("SELECT status FROM quotes WHERE id=?", (old["id"],))["status"] == "open"
    app.maker._polled[m.ticker] = now + 66
    await app.maker.expire()
    await app.om.reconcile_orders()
    assert app.db.one("SELECT status FROM quotes WHERE id=?", (old["id"],))["status"] == "expired"
    st = {r["client_order_id"]: r["state"] for r in app.db.q("SELECT * FROM orders")}
    assert st[old["client_order_id"]] == "canceled" and st[new["client_order_id"]] == "resting"


def test_quote_risk_gate_limits(db):
    r = RiskManager(db, RiskLimits(), "paper")
    assert r.approve_quote("T", "yes", 0.20, 1, 0.0).approved
    assert "budget" in r.approve_quote("T", "no", 0.76, 1, 0.0).reason
    db.x("INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,status) VALUES('paper','T','yes',1,0.2,0,'open')")
    assert "inventory" in r.approve_quote("T", "yes", 0.20, 1, 0.0).reason
    db.set("pause", {"reason": "test"})
    assert "halted" in r.approve_quote("U", "yes", 0.10, 1, 0.0).reason


async def test_paper_post_only_rests_and_never_crosses():
    b = book("T", 0.40, 0.45)
    a = PaperAdapter({"T": b}.get)
    eo = await a.submit(ticker="T", outcome_side="yes", limit_price=0.40, count=1, client_order_id="c1" * 8,
                        post_only=True)
    assert (eo.status, eo.filled, eo.maker) == ("resting", 0, True)
    cross = await a.submit(ticker="T", outcome_side="yes", limit_price=0.45, count=1, client_order_id="c2" * 8,
                           post_only=True)
    assert cross.status == "rejected"
    assert a.fill("c1" * 8, 0.40, 0.0) and a.orders["c1" * 8].status == "executed"
    assert not a.fill("c1" * 8, 0.40, 0.0)      # cannot fill twice


def test_maker_refuses_any_mode_but_paper(tmp_path):
    s = Settings(mode="demo", db_path=str(tmp_path / "d.sqlite"))
    s.strategy = StrategyConfig(name="maker")
    with pytest.raises(ValueError, match="paper mode only"):
        App(s)


async def test_history_reads_only_markets_before_the_holdout():
    db = DB(":memory:")
    for i, close in enumerate([HOLDOUT_START - 900, HOLDOUT_START + 900]):
        t = f"KXETH15M-H{i}"
        db.x("INSERT INTO markets(ticker,series,open_ts,close_ts,floor_strike,status,result) VALUES(?,?,?,?,?,?,?)",
             (t, "KXETH15M", close - 900, close, 100.0, "finalized", "yes"))
        for k in range(1, 15):
            db.x("INSERT INTO candles(ticker,end_ts,yes_bid,yes_ask) VALUES(?,?,?,?)", (t, close - 900 + 60 * k, 0.40, 0.42))
        db.x("INSERT INTO trades VALUES(?,?,?,?,?,?)", (t, close - 900 + 60 * 3 + 5, "no", 0.39, 2.0, 1))
    rep = await history(None, db, per_series={"KXETH15M": 5})
    assert rep["sample"]["with_trades"] == 1 and rep["markets"] == 1
    assert rep["primary"]["net_zero_fee"]["n"] == 1 and rep["primary"]["net_zero_fee"]["mean"] == pytest.approx(0.60)
    assert "HISTORICAL BACKTEST" in rep["label"]

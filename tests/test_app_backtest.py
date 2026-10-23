import json
import time

import httpx
import pytest

from kbot import backtest as BT
from kbot import config as K
from kbot.app import App
from kbot.backtest import Candle, Costs, MarketData, chronological_split, metrics, run, walk_forward
from kbot.client import ProductionDisabled
from kbot.config import RiskLimits, Settings, StrategyConfig
from kbot.dashboard import state
from kbot.discovery import MarketInfo
from kbot.feed import SpotStore, WsFeed
from kbot.strategy import Rejection

from conftest import FakePublic, api_market


# --- live app (paper) ---------------------------------------------------------
class FakeSpot:
    def __init__(self, store, price):
        self.store, self.price = store, price

    async def poll(self, series):
        now = time.time()
        if not self.store.points("BRTI"):
            for i in range(300, 0, -1):        # 5 minutes of history with small wiggles
                self.store.add("BRTI", now - i, self.price * (1 + (1e-4 if i % 2 else -1e-4)), "test")
        self.store.add("BRTI", now, self.price, "test")


def make_app(settings, price=100.2, yes_bid=0.30, yes_ask=0.32):
    now = time.time()
    pub = FakePublic({"KXBTC15M-A": api_market("KXBTC15M-A", now, close_in=600, strike=100.0)},
                     {"KXBTC15M-A": {"yes_dollars": [[f"{yes_bid:.2f}", "100.00"]],
                                     "no_dollars": [[f"{1 - yes_ask:.2f}", "100.00"]]}})
    app = App(settings)
    app.public = app.discovery.client = app.poller.client = pub
    app.coinbase = FakeSpot(app.spot, price)
    return app, pub


async def test_paper_tick_trades_records_and_settles(settings):
    app, pub = make_app(settings)
    app.startup_checks()
    await app.tick()
    orders = app.db.q("SELECT * FROM orders WHERE mode='paper'")
    assert len(orders) == 1 and orders[0]["outcome_side"] == "yes" and orders[0]["filled"] > 0
    sig = app.db.one("SELECT * FROM signals WHERE accepted=1")
    assert sig and json.loads(sig["data"])["net_edge"] >= settings.strategy.min_net_edge
    # Second tick: no duplicate order in the same market.
    await app.tick()
    assert len(app.db.q("SELECT * FROM orders")) == 1
    # Market settles YES.
    app.db.x("UPDATE positions SET close_ts=?", (time.time() - 60,))
    pub.results["KXBTC15M-A"] = "yes"
    await app.settle_finished()
    p = app.db.one("SELECT * FROM positions")
    assert p["status"] == "settled" and p["pnl"] == pytest.approx(p["count"] - p["cost"] - p["fees"])
    st = state(app.db, settings)
    assert st["performance"]["settled_trades"] == 1 and "SIMULATED" in st["mode_label"]


async def test_restart_does_not_duplicate_or_reopen(settings):
    app, _ = make_app(settings)
    app.startup_checks()
    await app.tick()
    app.db.close()                              # crash: no clean shutdown
    app2, _ = make_app(settings)
    app2.startup_checks()
    assert app2.db.q("SELECT * FROM events WHERE kind='unexpected_termination'")
    await app2.tick()
    assert len(app2.db.q("SELECT * FROM orders")) == 1


async def test_stale_book_blocks_entry(settings):
    app, _ = make_app(settings)
    await app.discovery.refresh()
    await app.poller.poll(["KXBTC15M-A"])
    await app.coinbase.poll(["KXBTC15M-A"])
    app.books["KXBTC15M-A"].updated_at -= 60
    m = app.discovery.markets["KXBTC15M-A"]
    await app.evaluate(m, time.time(), True, "ok")
    assert app.db.q("SELECT * FROM orders") == []
    assert "stale" in app.db.one("SELECT reason FROM signals ORDER BY id DESC")["reason"]


async def test_estop_and_pause_block_new_orders(settings):
    for key in ("estop", "pause"):
        settings.db_path = settings.db_path + key
        app, _ = make_app(settings)
        app.db.set(key, {"reason": "test"})
        await app.tick()
        assert app.db.q("SELECT * FROM orders") == []
        rej = app.db.one("SELECT reason FROM signals WHERE net_edge IS NOT NULL ORDER BY id DESC")
        assert key in rej["reason"] or "emergency" in rej["reason"]


async def test_unhealthy_api_blocks_entries(settings):
    app, pub = make_app(settings)
    await app.discovery.refresh()
    pub.last_ok = time.time() - 600
    pub.clock_skew_s = 9.0
    ok, reason = app.check_health(["KXBTC15M-A"])
    assert not ok and "clock skew" in reason and "60s" in reason
    pub.last_ok, pub.clock_skew_s = None, 0.0          # never reached the API at all
    assert not app.check_health(["KXBTC15M-A"])[0]


def test_paper_app_has_no_trading_client_and_cannot_post(settings, monkeypatch):
    """A simulated strategy must never be able to reach an order endpoint."""
    posts = []
    orig = httpx.AsyncClient.request

    async def guard(self, method, url, *a, **k):
        if method.upper() in ("POST", "DELETE"):
            posts.append(url)
            raise AssertionError("network order call from paper mode")
        return await orig(self, method, url, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "request", guard)
    app, _ = make_app(settings)
    assert app.om.adapter.mode == "paper" and app.account is None
    assert not hasattr(app.om.adapter, "client")
    import asyncio
    asyncio.run(app.tick())
    assert posts == []


def test_production_app_refuses_when_not_armed(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOT_MODE", "production")
    monkeypatch.setenv("KBOT_DATA_ENV", "production")
    monkeypatch.setenv("KBOT_DB_PATH", str(tmp_path / "p.sqlite"))
    s = K.load(str(tmp_path / "none.toml"))
    assert not s.production_armed()
    with pytest.raises((ProductionDisabled, ValueError)):
        App(s)


def test_dashboard_state_has_no_secrets(settings, monkeypatch):
    settings.api_key_id = "SECRET-KEY-ID-123"
    settings.private_key_path = "/home/me/secret.pem"
    from kbot.db import DB
    out = json.dumps(state(DB(settings.db_path), settings), default=str)
    assert "SECRET-KEY-ID-123" not in out and "secret.pem" not in out


# --- config ---------------------------------------------------------------------
def test_config_defaults_and_env(tmp_path, monkeypatch):
    s = K.load(str(tmp_path / "missing.toml"))
    assert s.mode == "paper" and not s.production_armed() and s.risk.bankroll == 25
    assert s.risk.max_position_risk_pct == 0.01 and s.risk.max_open_positions == 3
    monkeypatch.setenv("KBOT_MODE", "demo")
    monkeypatch.setenv("KBOT_DEMO_KEY_ID", "demo-id")
    monkeypatch.setenv("KBOT_PROD_KEY_ID", "prod-id")
    s = K.load(str(tmp_path / "missing.toml"))
    assert s.api_key_id == "demo-id" and s.data_env == "demo" and s.trade_env == "demo"


def test_config_rejects_secrets_in_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('api_key_id = "x"\n')
    with pytest.raises(ValueError):
        K.load(str(p))
    p.write_text('mode = "paper"\n[risk]\nmax_daily_loss_pct = 0.9\n')
    with pytest.raises(ValueError):
        K.load(str(p))


# --- websocket feed -------------------------------------------------------------
def test_ws_snapshot_delta_and_seq_gap():
    books, spot = {}, SpotStore()
    f = WsFeed("wss://x", None, books, spot)
    assert f.handle(json.dumps({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": "T", "yes_dollars_fp": [["0.40", "10.00"]], "no_dollars_fp": [["0.55", "5.00"]]}}))
    assert books["T"].best_ask("yes") == (0.45, 5)
    assert f.handle(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {
        "market_ticker": "T", "price_dollars": "0.56", "delta_fp": "3.00", "side": "no"}}))
    assert books["T"].best_ask("yes") == (0.44, 3)
    assert not f.handle(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 5, "msg": {
        "market_ticker": "T", "price_dollars": "0.56", "delta_fp": "1.00", "side": "no"}}))
    f.handle(json.dumps({"type": "cfbenchmarks_value", "sid": 2, "seq": 1, "msg": {
        "index_id": "BRTI", "data": json.dumps({"time": 1710000000123, "value": "68000.12"}),
        "avg_60s_data": {"value": "68001.00"}}}))
    assert spot.latest("BRTI") == (1710000000.123, 68000.12) and spot.avg60["BRTI"][1] == 68001.0


# --- backtest -------------------------------------------------------------------
def synthetic_markets(n=60, start=1_700_000_000):
    """TEST INPUT ONLY. Alternating outcomes with a fixed quote, so results are hand-checkable."""
    out = []
    for i in range(n):
        o = start + i * 900
        info = MarketInfo(f"KXBTC15M-{i}", "KXBTC15M", "E", o, o + 900, "active", 100.0, None, "")
        candles = [Candle(o + 60 * k, 0.30, 0.32) for k in range(1, 15)]
        out.append(MarketData(info, candles, "yes" if i % 2 == 0 else "no"))
    return out


def test_backtest_no_lookahead(monkeypatch):
    seen = []

    class Spy:
        name = "spy"

        def evaluate(self, snap):
            seen.append((snap.now, max(t for t, _ in snap.mid_history), snap.spot_ts))
            return Rejection(snap.market.ticker, "spy", "observe only")
    monkeypatch.setattr(BT, "make", lambda name, cfg: Spy())
    spot = {"BRTI": [(1_700_000_000 + 30 * k, 100.0) for k in range(2000)]}
    run(synthetic_markets(5), "spy", StrategyConfig(), RiskLimits(bankroll=1000), Costs(),
        spot, {"KXBTC15M": "BRTI"})
    assert seen and all(h <= now and (st is None or st <= now) for now, h, st in seen)


def test_backtest_pnl_matches_hand_calculation():
    # Fair value with spot far above strike -> buys YES at 0.32 + 0.01 slippage on every market.
    ms = synthetic_markets(20)
    spot = {"BRTI": [(1_700_000_000 + 10 * k, 100.5 + (0.01 if k % 2 else 0)) for k in range(2000)]}
    r = run(ms, "fair_value", StrategyConfig(min_seconds_left=60), RiskLimits(bankroll=1000, max_consecutive_losses=99),
            Costs(fill_prob=1.0, slippage=0.01, assumed_depth=10, fill_fraction=0.5), spot, {"KXBTC15M": "BRTI"})
    assert len(r.trades) == 20 and all(t.side == "yes" and t.price == 0.33 and t.count == 5 for t in r.trades)
    f = 0.08    # fee(0.33, 5) = ceil(0.07*5*0.33*0.67 = 0.0774) = 0.08
    expect = 10 * (5 - 5 * 0.33 - f) + 10 * (-5 * 0.33 - f)
    m = metrics(r)
    assert m["total_net_pnl"] == pytest.approx(expect, abs=1e-6)
    assert m["win_rate"] == 0.5 and m["total_fees"] == pytest.approx(20 * f)
    assert m["independent_windows"] == 20 and m["ci95_avg_net_per_trade"] is not None


def test_backtest_fee_and_fill_stress_lowers_pnl():
    ms = synthetic_markets(20)
    spot = {"BRTI": [(1_700_000_000 + 10 * k, 100.5 + (0.01 if k % 2 else 0)) for k in range(2000)]}
    args = (ms, "fair_value", StrategyConfig(min_seconds_left=60), RiskLimits(bankroll=1000, max_consecutive_losses=99))
    base = metrics(run(*args, Costs(fill_prob=1.0), spot, {"KXBTC15M": "BRTI"}))
    worse = metrics(run(*args, Costs(fill_prob=1.0, fee_multiplier=2, slippage=0.03), spot, {"KXBTC15M": "BRTI"}))
    assert worse["total_net_pnl"] < base["total_net_pnl"]


def test_backtest_daily_loss_limit_trips():
    ms = [m for m in synthetic_markets(40) if m.result == "no"]   # every YES buy loses
    spot = {"BRTI": [(1_700_000_000 + 10 * k, 100.5 + (0.01 if k % 2 else 0)) for k in range(5000)]}
    r = run(ms, "fair_value", StrategyConfig(min_seconds_left=60), RiskLimits(bankroll=100),
            Costs(fill_prob=1.0), spot, {"KXBTC15M": "BRTI"})
    assert r.trips
    assert -sum(t.pnl for t in r.trades) <= 0.02 * 100 * (len({BT.utc_day(t.close_ts) for t in r.trades}) + 1) + 1


def test_splits_are_chronological_and_disjoint():
    ms = synthetic_markets(50)
    tr, va, ho = chronological_split(ms)
    assert tr[-1].info.close_ts < va[0].info.close_ts < ho[0].info.close_ts
    folds = walk_forward(tr + va, 4)
    assert sum(len(f) for f in folds) == len(tr + va)


def test_research_refuses_conclusions_without_data(settings, tmp_path):
    from kbot.db import DB
    from kbot.research import research
    rep = research(DB(settings.db_path), settings, out_dir=str(tmp_path))
    assert rep["verdict"].startswith("INSUFFICIENT DATA")


def test_research_pipeline_runs_end_to_end_on_test_input(settings, tmp_path):
    from kbot.db import DB
    from kbot.research import research
    db = DB(settings.db_path)
    for md in synthetic_markets(80):
        i = md.info
        db.x("INSERT INTO markets(ticker,series,open_ts,close_ts,floor_strike,status,result) VALUES(?,?,?,?,?,?,?)",
             (i.ticker, i.series, i.open_ts, i.close_ts, 100.0, "finalized", md.result))
        for c in md.candles:
            db.x("INSERT INTO candles(ticker,end_ts,yes_bid,yes_ask) VALUES(?,?,?,?)",
                 (i.ticker, c.end_ts, c.yes_bid, c.yes_ask))
    for k in range(8000):
        db.x("INSERT INTO spot(index_id,ts,value) VALUES('BRTI',?,?)", (1_700_000_000 + 10 * k, 100.5 + (k % 2) * 0.01))
    rep = research(db, settings, out_dir=str(tmp_path))
    assert rep["split"]["holdout"] == 16 and "holdout" not in rep
    assert rep["calibration_train"]["5min_left"]["n"] > 0
    assert set(rep["validation"]["results"]) >= {"fair_value", "no_trade", "market_implied"}
    assert rep["validation"]["results"]["market_implied"]["trades"] == 0
    assert rep["verdict"].startswith("NO STRATEGY PASSED")
    rep2 = research(db, settings, open_holdout=True, out_dir=str(tmp_path))
    assert rep2["holdout_opened_count"] == 1


async def test_run_loop_survives_network_failure_and_shuts_down_cleanly(settings):
    import asyncio
    app, pub = make_app(settings)
    settings.loop_interval_s = 0.01
    calls = {"n": 0}

    async def broken(**_):
        calls["n"] += 1
        if calls["n"] >= 3:
            app.stop()
        raise httpx.ConnectError("network down")
        yield  # pragma: no cover
    pub.markets = broken
    app._every = lambda name, s: True       # force discovery every tick
    await asyncio.wait_for(app.run(), timeout=5)
    assert calls["n"] >= 3
    assert app.db.q("SELECT * FROM events WHERE kind='error'")
    assert app.db.get("running") is None and app.db.q("SELECT * FROM events WHERE kind='shutdown'")
    assert app.db.get("health")["ok"] is False
    assert app.db.q("SELECT * FROM orders") == []

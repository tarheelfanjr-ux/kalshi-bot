"""Prospective experiments, recorder and trade backfill on hand-made inputs (not real Kalshi data)."""
import json
import math

import httpx
import pytest
from conftest import FakePublic, api_market

from kbot import experiments as ex
from kbot.collector import collect_trades
from kbot.db import DB
from kbot.model import realized_sigma
from kbot.recorder import Recorder

C = ex.PROSPECTIVE_START + 3 * 86400          # a window close inside the prospective period
M = {"ticker": "KXBTC15M-X", "series": "KXBTC15M", "open_ts": C - 900, "close_ts": C, "y": 0}


def fills(trades, candles=None, **kw):
    e = C - 300
    return ex.e2_fills(M, candles or [(e, 0.40, 0.42)], sorted(trades), **kw)


def test_e2_fill_rules_need_a_trade_against_our_bid_after_latency():
    e = C - 300
    trades = [(e, "no", 0.39, 5),          # inside the 1 s latency: we were not resting yet
              (e + 5, "no", 0.40, 3),      # at our YES bid
              (e + 10, "yes", 0.42, 5),    # taker buys YES at 0.42 = hits NO bids at 0.58 (ours)
              (e + 20, "no", 0.40, 8),     # more at our YES bid
              (e + 30, "no", 0.39, 1)]     # through our YES bid
    through = {f["side"]: f["ts"] for f in fills(trades)}
    assert through == {"yes": e + 30}      # NO never traded through 0.58
    touch = {f["side"]: f["ts"] for f in fills(trades, fill="touch")}
    assert touch == {"yes": e + 5, "no": e + 10}
    snaps = [(e - 1, 0.40, 0.42, 10.0, 50.0)]          # 10 contracts ahead on YES, 50 on NO
    queue = {f["side"]: (f["ts"], f["queue"]) for f in fills(trades, snaps=snaps, fill="queue")}
    assert queue == {"yes": (e + 20, 10.0)}             # 3 + 8 > 10 ahead; NO queue never cleared


def test_e2_quotes_only_inside_window_and_band_and_fills_once_per_side():
    e = C - 300
    candles = [(C - 30, 0.40, 0.42),                   # 30 s left: past the quoting window
               (e, 0.03, 0.05),                        # YES bid outside the 0.05 to 0.95 band
               (e + 60, 0.40, 0.42), (e + 120, 0.40, 0.42)]
    trades = [(e + 5, "no", 0.02, 1), (e + 65, "no", 0.39, 1), (e + 125, "no", 0.39, 1)]
    got = ex.e2_fills(M, sorted(candles), trades)
    assert [(f["side"], f["ts"]) for f in got] == [("yes", e + 65)]


def test_e2_rows_split_pnl_into_spread_and_adverse_selection():
    f = {"side": "yes", "price": 0.40, "quote_ts": C - 300, "ts": C - 290, "left": 300, "mid": 0.41,
         "spread": 0.02, "queue": None}
    r = ex._e2_rows({**M, "prev_move": None}, [f], [(C - 230, 0.30)], {})[0]
    assert r["gross"] == pytest.approx(-0.40) and r["net_zero"] == pytest.approx(-0.40)
    assert r["spread_earned"] == pytest.approx(0.01) and r["adverse_selection"] == pytest.approx(-0.41)
    assert r["markout_60s"] == pytest.approx(-0.10)      # mid 0.30 a minute later
    assert r["markout_300s"] == pytest.approx(-0.40)     # past the close: worth the outcome
    assert r["net_maker_exact"] == pytest.approx(-0.40 - 0.0175 * 0.24)
    assert r["net_maker_cent"] == pytest.approx(-0.41)


def _ticks(t0, n, start=100.0, step=0.01):
    return [(t0 + i, start + step * math.sin(i)) for i in range(n)]


def test_tick_forecast_uses_only_ticks_received_by_t():
    t = C - 120
    ticks = _ticks(t - 900, 901)
    spec = ex.SPECS["E1"]
    base = ex.TickWindow(ticks).forecast(t, C, 100.0, spec)
    jumped = ex.TickWindow(sorted(ticks + [(t + 0.5, 150.0)])).forecast(t, C, 100.0, spec)
    assert base == jumped
    sigma = realized_sigma(ticks, 900, now=t, min_points=300)
    from kbot.model import prob_settle_yes
    assert base == pytest.approx(prob_settle_yes(ticks[-1][1], 100.0, 120, sigma))
    assert ex.TickWindow(ticks[:-10]).forecast(t, C, 100.0, spec) is None     # newest tick 10 s old
    assert ex.TickWindow(ticks[-200:]).forecast(t, C, 100.0, spec) is None    # too few ticks for volatility


def test_tick_forecast_uses_the_fixed_part_of_the_average_in_the_final_minute():
    t = C - 20
    ticks = _ticks(t - 900, 860) + [(C - 60 + i, 101.0) for i in range(41)]
    q = ex.TickWindow(ticks).forecast(t, C, 100.5, ex.SPECS["E1"])
    assert q > 0.99        # 40 s of the 60 s average already sit at 101 against a 100.5 strike


def test_e1_trade_is_ioc_at_the_limit_and_retries_at_the_next_checkpoint():
    coef = [0.0, 1.0, 1.0]           # blend = the arm's own forecast
    rows = [{"c": 300, "t": C - 300, "bid": 0.48, "ask": 0.50, "mid": 0.49, "spread": 0.02, "cfbenchmarks": 0.60},
            {"c": 120, "t": C - 120, "bid": 0.48, "ask": 0.50, "mid": 0.49, "spread": 0.02, "cfbenchmarks": 0.60}]
    snaps = [(C - 300.5, 0.48, 0.50), (C - 298.0, 0.57, 0.59),   # ask jumped past the limit before we arrive
             (C - 118.5, 0.48, 0.51)]
    tr = ex.e1_trade({**M, "y": 1}, rows, snaps, coef, "cfbenchmarks")
    assert tr["c"] == 120 and tr["side"] == "yes" and tr["price"] == 0.51
    assert tr["net"] == pytest.approx(1 - 0.51 - 0.07 * 0.51 * 0.49)
    assert ex.e1_trade({**M, "y": 1}, rows, snaps, coef, "cfbenchmarks", fee_mode="cent")["fee"] == 0.02


def _db_with_market(open_ts, ticker="KXBTC15M-A", result="yes"):
    db = DB(":memory:")
    db.x("INSERT INTO markets(ticker,series,open_ts,close_ts,floor_strike,result,data) VALUES(?,?,?,?,?,?,?)",
         (ticker, "KXBTC15M", open_ts, open_ts + 900, 100.0, result, json.dumps({"expiration_value": "101"})))
    return db


def test_markets_before_the_prospective_start_are_never_read():
    db = _db_with_market(ex.PROSPECTIVE_START - 900)
    assert ex.markets(db, 0, 2e10) == []
    db = _db_with_market(ex.PROSPECTIVE_START)
    assert [m["ticker"] for m in ex.markets(db, 0, 2e10)] == ["KXBTC15M-A"]


def test_status_shows_coverage_only_and_e1_waits_for_the_key(tmp_path):
    db = _db_with_market(ex.PROSPECTIVE_START)
    st = ex.status(db, now=ex.PROSPECTIVE_START + 86400)
    assert st["E1"]["state"] == "not started" and "READONLY_KEY" in st["E1"]["why"]
    assert st["E2"]["state"] == "collecting" and st["E2"]["coverage"]["settled_markets"] == 1
    flat = json.dumps(st)
    assert "result" not in flat and "net" not in flat and "pass" not in flat


def test_evaluate_refuses_before_the_end_and_counts_every_opening(tmp_path):
    db = _db_with_market(ex.PROSPECTIVE_START)
    now = ex.PROSPECTIVE_START + 5 * 86400
    with pytest.raises(SystemExit, match="pending"):
        ex.evaluate(db, "E2", now=now, out_dir=str(tmp_path))
    with pytest.raises(SystemExit, match="pending"):
        ex.evaluate(db, "E2", final=True, now=now, out_dir=str(tmp_path))
    rep = ex.evaluate(db, "E2", final=True, force=True, now=now, out_dir=str(tmp_path))
    assert rep["final_openings"] == 1 and "EARLY LOOK" in rep["warning"]
    assert rep["spec_hash"] == ex.spec_hash("E2") and rep["results"]["pass"]["passed"] is False
    rep = ex.evaluate(db, "E2", final=True, force=True, now=now, out_dir=str(tmp_path))
    assert rep["final_openings"] == 2
    with pytest.raises(SystemExit, match="interim look opens"):
        ex.evaluate(db, "E2", interim=True, now=now, out_dir=str(tmp_path))


def test_e2_report_end_to_end_on_hand_made_windows():
    db = DB(":memory:")
    for k in range(8):
        close = ex.PROSPECTIVE_START + 900 * (k + 2)
        t = f"KXETH15M-{k}"
        db.x("INSERT INTO markets(ticker,series,open_ts,close_ts,floor_strike,result,data) VALUES(?,?,?,?,?,?,?)",
             (t, "KXETH15M", close - 900, close, 100.0 + k, "yes" if k % 2 else "no", "{}"))
        for e in range(int(close - 840), int(close - 59), 60):
            db.x("INSERT INTO candles(ticker,end_ts,yes_bid,yes_ask) VALUES(?,?,?,?)", (t, e, 0.49, 0.51))
            db.x("INSERT INTO trades VALUES(?,?,?,?,?,?)", (t, e + 5, "no", 0.48, 1.0, 1))
            db.x("INSERT INTO trades VALUES(?,?,?,?,?,?)", (t, e + 7, "yes", 0.52, 1.0, 1))
    rep = ex.e2_report(db, ex.PROSPECTIVE_START, ex.PROSPECTIVE_START + 86400, 0.975)
    p = rep["primary"]["net_zero_fee"]
    assert p["n"] == 16 and p["windows"] == 8 and p["ci_level"] == 0.975
    assert p["mean"] == pytest.approx(0.01)       # one YES and one NO fill per market lock in the spread
    assert rep["missing"] == {"no recorded book (book variants only)": 8}
    assert set(rep["strata"]) >= {"series", "week", "half", "time_left", "fill_price", "us_market_hours"}


class _Trades:
    def __init__(self, cutoff, by_endpoint):
        self.cutoff, self.by = cutoff, by_endpoint
        self.calls = []

    async def historical_cutoff(self):
        return {"trades_created_ts": self.cutoff}

    async def trades(self, historical=False, **params):
        self.calls.append((historical, params["ticker"]))
        for t in self.by.get((historical, params["ticker"]), []):
            yield t


def _trade(ts_iso, side, price, count):
    return {"created_time": ts_iso, "taker_side": side, "yes_price_dollars": str(price), "count_fp": str(count)}


async def test_collect_trades_sums_per_second_and_reads_both_sides_of_the_cutoff():
    db = _db_with_market(ex.PROSPECTIVE_START)
    db.x("UPDATE markets SET open_ts=?, close_ts=? WHERE ticker='KXBTC15M-A'",
         (ex.PROSPECTIVE_START, ex.PROSPECTIVE_START + 900))
    cutoff_iso = "2026-10-24T00:10:00Z"           # inside the market's window
    fake = _Trades(cutoff_iso, {
        (True, "KXBTC15M-A"): [_trade("2026-10-24T00:05:00.100Z", "yes", 0.5, 2),
                               _trade("2026-10-24T00:05:00.900Z", "yes", 0.5, 3)],
        (False, "KXBTC15M-A"): [_trade("2026-10-24T00:12:00Z", "no", 0.45, 1)]})
    assert await collect_trades(fake, db, ex.PROSPECTIVE_START) == 1
    rows = [tuple(r) for r in db.q("SELECT ts, taker_side, yes_price, count, n FROM trades ORDER BY ts")]
    assert rows == [(ex.PROSPECTIVE_START + 300, "yes", 0.5, 5.0, 2), (ex.PROSPECTIVE_START + 720, "no", 0.45, 1.0, 1)]
    assert await collect_trades(fake, db, ex.PROSPECTIVE_START) == 0    # resumable


class _Public(FakePublic):
    async def markets(self, **params):
        for m in self.markets_data.values():
            if m["ticker"].startswith(params.get("series_ticker", "")):
                yield m

    async def fee_changes(self, series_ticker=None):
        return []

    async def paginate(self, path, key, params=None, auth=False):
        for x in []:
            yield x


async def test_recorder_writes_books_coinbase_and_rti_ticks_with_receive_time(settings):
    import time
    now = time.time()
    m = api_market("KXBTC15M-R", now, close_in=600)
    pub = _Public({"KXBTC15M-R": m}, {"KXBTC15M-R": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.58", "7"]]}})
    cb = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"price": "81000.5", "time": "2026-10-23T18:00:48.765257257Z"})))
    db = DB(":memory:")
    rec = Recorder(settings, db, public=pub, slow=pub, http=cb)
    await rec.discovery.refresh()
    await rec.log_fees()
    await rec.tick(now)
    await rec.tick(now + 2)                     # unchanged book inside 10 s: not written again
    snaps = db.q("SELECT ticker, yes_bid, yes_ask, yes_bid_size, yes_ask_size FROM snapshots")
    assert [tuple(r) for r in snaps] == [("KXBTC15M-R", 0.40, 0.42, 10.0, 7.0)]
    cbt = db.q("SELECT index_id, source, value FROM index_ticks")
    assert {tuple(r) for r in cbt} == {("BTC-USD", "coinbase", 81000.5)}
    rec.on_index({"index_id": "BRTI", "avg_60s_data": {"value": "80999.1"},
                  "last_60s_windowed_average_15min": {"value": "80998.0"}},
                 {"value": "81001.0", "time": 1792778448765}, now + 3)
    r = db.one("SELECT * FROM index_ticks WHERE source='cfbenchmarks'")
    assert (r["recv_ts"], r["src_ts"], r["value"], r["avg60"], r["final_avg"]) == (
        now + 3, 1792778448.765, 81001.0, 80999.1, 80998.0)
    assert ("orderbooks", ("KXBTC15M-R",)) in pub.calls and db.get("recorder_heartbeat") == now + 2
    assert db.one("SELECT 1 FROM events WHERE kind='fee_state'") is not None


async def test_recorder_logs_downtime_and_marks_itself_alive_before_the_first_tick(settings):
    import asyncio, time
    db = DB(":memory:")
    db.set("recorder_heartbeat", time.time() - 300)
    rec = Recorder(settings, db, public=_Public({}, {}), slow=_Public({}, {}), http=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(0.05)
    assert time.time() - db.get("recorder_heartbeat") < 5      # a crash from here on still shows as downtime
    rec._stop.set()
    await task
    down = json.loads(db.one("SELECT data FROM events WHERE kind='interruption'")["data"])
    assert down["component"] == "recorder" and down["down_s"] >= 300
    assert ex.status(db)["recorder_restarts"]["count"] == 1


async def test_recorder_also_records_candidate_series_but_experiments_ignore_them(settings):
    import time
    from kbot.experiments import coverage
    now = time.time()
    ms = {t: api_market(t, now, close_in=600) for t in ("KXBTC15M-R", "KXEURUSD15M-R")}
    ms["KXEURUSD15M-R"]["series_ticker"] = "KXEURUSD15M"
    book = {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.58", "7"]]}
    pub = _Public(ms, {t: book for t in ms})
    cb = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"price": "81000.5", "time": "2026-10-23T18:00:48Z"})))
    db = DB(":memory:")
    rec = Recorder(settings, db, public=pub, slow=pub, http=cb)
    await rec.discovery.refresh()
    await rec.tick(now)
    assert {r["ticker"] for r in db.q("SELECT ticker FROM snapshots")} == set(ms)
    assert {r["index_id"] for r in db.q("SELECT index_id FROM index_ticks")} == {"BTC-USD"}   # no Coinbase feed for FX
    for t in ms:
        db.x("UPDATE markets SET result='yes', open_ts=? WHERE ticker=?", (1792800000, t))
    cov, _ = coverage(db, "E2", 0, 1792900000)
    assert cov["settled_markets"] == 1

"""Collector and data audit on hand-made API responses (not real Kalshi data)."""
from conftest import iso

from kbot.collector import collect_history, load_markets, verify_history
from kbot.db import DB

# 2026-10-23 10:00 and 10:15 US Eastern (EDT) as UTC seconds
T1, T2 = 1792764000, 1792764900


def api(ticker, close, strike, exp, result):
    return {"ticker": ticker, "event_ticker": ticker[:-3], "open_time": iso(close - 900), "close_time": iso(close),
            "status": "finalized", "result": result, "floor_strike": strike, "strike_type": "greater_or_equal",
            "expiration_value": str(exp), "settlement_ts": iso(close + 3)}


def candle(end, bid, ask):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": str(bid)}, "yes_ask": {"close_dollars": str(ask)},
            "price": {}, "volume_fp": "1.00", "open_interest_fp": "1.00"}


class FakeHistory:
    def __init__(self, markets, candles):
        self.ms, self.cs, self.batch_calls, self.old = markets, candles, [], set()

    async def series(self, s):
        return {"fee_type": "quadratic", "fee_multiplier": 1}

    async def _gen(self, items):
        for m in items:
            yield m

    def markets(self, **_):
        return self._gen(self.ms)

    def historical_markets(self, **_):
        return self._gen([])

    async def batch_candlesticks(self, tickers, start, end, period=1):
        self.batch_calls.append(tickers)
        return {t: self.cs.get(t, []) for t in tickers if t not in self.old}

    async def historical_candlesticks(self, ticker, start, end, period=1):
        # The single historical endpoint drops the _dollars / _fp suffixes.
        return [{"end_period_ts": c["end_period_ts"], "yes_bid": {"close": c["yes_bid"]["close_dollars"]},
                 "yes_ask": {"close": c["yes_ask"]["close_dollars"]}, "volume": "2.00"}
                for c in self.cs.get(ticker, [])]


async def test_collect_batches_candles_and_is_resumable():
    db = DB(":memory:")
    ms = [api("KXBTC15M-26OCT231015-15", T2, 100.0, 99.0, "no"),
          api("KXBTC15M-26OCT231000-00", T1, 99.5, 100.0, "yes")]
    cs = {"KXBTC15M-26OCT231000-00": [candle(T1 - 600, 0.4, 0.42), candle(T1 + 5000, 0.5, 0.52)],
          "KXBTC15M-26OCT231015-15": [candle(T2 - 600, 0.6, 0.61)]}
    fake = FakeHistory(ms, cs)
    assert await collect_history(fake, db, ["KXBTC15M"]) == {"KXBTC15M": 2}
    assert len(fake.batch_calls) == 1
    md = {m.info.ticker: m for m in load_markets(db)}
    assert [c.end_ts for c in md["KXBTC15M-26OCT231000-00"].candles] == [T1 - 600]   # out-of-window candle dropped
    await collect_history(fake, db, ["KXBTC15M"])
    assert len(fake.batch_calls) == 1                                               # nothing left to fetch


async def test_collect_falls_back_to_historical_endpoint_for_old_markets():
    db = DB(":memory:")
    fake = FakeHistory([api("KXBTC15M-26OCT231000-00", T1, 99.5, 100.0, "yes")],
                       {"KXBTC15M-26OCT231000-00": [candle(T1 - 600, 0.4, 0.42)]})
    fake.old = {"KXBTC15M-26OCT231000-00"}
    await collect_history(fake, db, ["KXBTC15M"])
    r = db.one("SELECT yes_bid, yes_ask, volume FROM candles")
    assert (r["yes_bid"], r["yes_ask"], r["volume"]) == (0.4, 0.42, 2.0)


async def test_verify_flags_result_and_timing_problems():
    db = DB(":memory:")
    ms = [api("KXBTC15M-26OCT231000-00", T1, 99.5, 100.0, "yes"),
          api("KXBTC15M-26OCT231015-15", T2, 100.0, 101.0, "no"),        # wrong: 101 >= 100 should be yes
          api("KXBTC15M-26OCT231100-00", T2 + 2700, 101.0, 100.0, "no")]  # ticker 11:00 ET, so 2 windows missing
    fake = FakeHistory(ms, {})
    await collect_history(fake, db, ["KXBTC15M"])
    c = verify_history(db)["KXBTC15M"]["checks"]
    assert c["result_matches_rule"] == 2 and c["result_mismatch"] == 1
    assert c["missing_windows"] == 2
    assert c["strike_eq_prev_expiration"] == 1
    assert "ticker_time_mismatch" not in c


def test_candle_batches_respect_kalshi_cap():
    from kbot.collector import _batch_window, _candle_batches
    from kbot.discovery import MarketInfo
    ms = [MarketInfo(f"M{i}", "S", "E", T1 + i * 900 - 900, T1 + i * 900, "finalized", 1.0, None, "yes")
          for i in range(200)]
    batches = list(_candle_batches(ms))
    assert sum(len(b) for b in batches) == 200
    for b in batches:
        start, end = _batch_window(b)
        assert len(b) * ((end - start) // 60 + 1) <= 10000
    assert len(batches[0]) == 25

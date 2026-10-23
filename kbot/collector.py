"""Historical data collection into SQLite, and loading it back for backtests.

Sources (all real, nothing synthesized):
  * Kalshi settled markets for each 15M series (live + /historical endpoints)
  * Kalshi 1-minute candlesticks for each market (top-of-book bid/ask per minute)
  * Coinbase 1-minute candles as a *proxy* for the CF Benchmarks settlement index
  * Any CSV of (timestamp, price) the operator supplies, e.g. licensed index data
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .backtest import Candle, MarketData
from .client import PublicClient
from .db import DB
from .discovery import MarketInfo, is_short_window, num

log = logging.getLogger(__name__)


def _candle_row(ticker: str, c: dict) -> tuple:
    # Live/batch candles use *_dollars and *_fp keys; the single historical endpoint drops the suffixes.
    def g(d, k, suffix="_dollars"):
        return num((d or {}).get(k + suffix) or (d or {}).get(k))
    yb, ya, pr = c.get("yes_bid"), c.get("yes_ask"), c.get("price")
    return (ticker, int(c["end_period_ts"]), g(yb, "close"), g(ya, "close"), g(yb, "low"), g(ya, "high"),
            g(pr, "close"), g(c, "volume", "_fp"), g(c, "open_interest", "_fp"))


def _batch_window(ms: list[MarketInfo]) -> tuple[int, int]:
    return int(min(m.open_ts for m in ms)) - 60, int(max(m.close_ts for m in ms)) + 60


def _candle_batches(todo: list[MarketInfo], max_tickers: int = 100, max_candles: int = 10000):
    """Kalshi counts tickers x (end - start) minutes against the 10,000 candle cap, so a batch
    of consecutive 15-minute markets tops out near 25 tickers."""
    chunk: list[MarketInfo] = []
    for m in todo:
        trial = chunk + [m]
        start, end = _batch_window(trial)
        if chunk and (len(trial) > max_tickers or len(trial) * ((end - start) // 60 + 1) > max_candles):
            yield chunk
            trial = [m]
        chunk = trial
    if chunk:
        yield chunk


async def collect_history(client: PublicClient, db: DB, series: list[str], max_markets: int = 5000,
                          since_ts: float = 0) -> dict[str, int]:
    """Download settled short-window markets and their minute candles. Resumable:
    markets whose candles are already stored are skipped."""
    counts = {}
    for s in series:
        meta = await client.series(s) or {}
        seen = 0
        todo: list[MarketInfo] = []
        sources = [client.markets(series_ticker=s, status="settled"), client.historical_markets(series_ticker=s)]
        for it in sources:
            async for m in it:
                info = MarketInfo.from_api(m, s, meta.get("fee_type", "quadratic"),
                                           float(meta.get("fee_multiplier") or 1.0))
                if not is_short_window(info) or info.result not in ("yes", "no") or info.close_ts < since_ts:
                    continue
                # Kept for auditing settlement: result should equal expiration_value vs floor_strike.
                audit = json.dumps({k: m.get(k) for k in ("expiration_value", "strike_type", "settlement_ts",
                                                          "volume_fp", "settlement_value_dollars")})
                db.x("INSERT INTO markets(ticker,series,event_ticker,open_ts,close_ts,floor_strike,status,result,"
                     "fee_type,fee_multiplier,updated_at,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(ticker) DO UPDATE SET status=excluded.status, result=excluded.result, "
                     "data=excluded.data",
                     (info.ticker, s, info.event_ticker, info.open_ts, info.close_ts, info.floor_strike,
                      info.status, info.result, info.fee_type, info.fee_multiplier, time.time(), audit))
                if not db.one("SELECT 1 FROM candles WHERE ticker=? LIMIT 1", (info.ticker,)):
                    todo.append(info)
                seen += 1
                if seen >= max_markets:
                    break
            if seen >= max_markets:
                break
        todo.sort(key=lambda m: m.close_ts)
        done = 0
        for chunk in _candle_batches(todo):
            got = await client.batch_candlesticks([m.ticker for m in chunk], *_batch_window(chunk))
            for m in chunk:
                if not got.get(m.ticker):
                    got[m.ticker] = await client.historical_candlesticks(m.ticker, *_batch_window([m]))
            by_ticker = {m.ticker: m for m in chunk}
            with db.tx():
                for ticker, cs in got.items():
                    m = by_ticker.get(ticker)
                    for c in cs:
                        if m and m.open_ts - 60 <= c["end_period_ts"] <= m.close_ts + 60:
                            db.x("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?)", _candle_row(ticker, c))
            done += len(chunk)
            log.info("%s: candles for %d/%d markets", s, done, len(todo))
        counts[s] = seen
        log.info("%s: %d markets with candles", s, seen)
    db.event("collect_history", series=series, counts=counts)
    return counts


async def collect_coinbase_spot(db: DB, product: str, index_id: str, start: float, end: float,
                                http: httpx.AsyncClient | None = None) -> int:
    """1-minute Coinbase candles (close price at candle end) as an index proxy."""
    http = http or httpx.AsyncClient(timeout=10)
    n, t = 0, start
    while t < end:
        t2 = min(end, t + 300 * 60)
        r = await http.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                           params={"granularity": 60,
                                   "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                                   "end": datetime.fromtimestamp(t2, timezone.utc).isoformat()})
        r.raise_for_status()
        with db.tx():
            for row in r.json():   # [time, low, high, open, close, volume], time = candle start
                db.x("INSERT OR IGNORE INTO spot(index_id,ts,value) VALUES(?,?,?)",
                     (index_id, float(row[0]) + 60, float(row[4])))
                n += 1
        t = t2
        await asyncio.sleep(0.35)  # stay well under Coinbase public limits
    return n


def import_spot_csv(db: DB, path: str, index_id: str) -> int:
    """CSV with columns ts (unix seconds or ISO 8601, UTC) and price."""
    n = 0
    with open(path) as f, db.tx():
        for row in csv.DictReader(f):
            raw = row.get("ts") or row.get("timestamp")
            try:
                ts = float(raw)
            except ValueError:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            db.x("INSERT OR IGNORE INTO spot(index_id,ts,value) VALUES(?,?,?)",
                 (index_id, ts, float(row["price"])))
            n += 1
    return n


def load_markets(db: DB, series: list[str] | None = None) -> list[MarketData]:
    sql = "SELECT * FROM markets WHERE result IN ('yes','no')"
    args: tuple = ()
    if series:
        sql += f" AND series IN ({','.join('?' * len(series))})"
        args = tuple(series)
    out = []
    for m in db.q(sql + " ORDER BY close_ts", args):
        candles = [Candle(r["end_ts"], r["yes_bid"], r["yes_ask"])
                   for r in db.q("SELECT end_ts,yes_bid,yes_ask FROM candles WHERE ticker=? ORDER BY end_ts",
                                 (m["ticker"],))]
        if not candles:
            continue
        info = MarketInfo(m["ticker"], m["series"], m["event_ticker"] or "", m["open_ts"], m["close_ts"],
                          "active", m["floor_strike"], None, m["result"], m["fee_type"] or "quadratic",
                          m["fee_multiplier"] or 1.0)
        out.append(MarketData(info, candles, m["result"]))
    return out


def load_spot(db: DB) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list] = {}
    for r in db.q("SELECT index_id, ts, value FROM spot ORDER BY index_id, ts"):
        out.setdefault(r["index_id"], []).append((r["ts"], r["value"]))
    return out


_TICKER_TIME = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})(?:-|$)")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def verify_history(db: DB, series: list[str] | None = None) -> dict:
    """Audit stored history: window timing, ticker time vs close time (US Eastern),
    official result vs expiration_value and strike, strike continuity, gaps, and quotes."""
    et = ZoneInfo("America/New_York")
    sql = "SELECT * FROM markets WHERE result IN ('yes','no')"
    args: tuple = ()
    if series:
        sql += f" AND series IN ({','.join('?' * len(series))})"
        args = tuple(series)
    rows = db.q(sql + " ORDER BY series, close_ts", args)
    candles: dict[str, list] = defaultdict(list)
    for r in db.q("SELECT ticker, end_ts, yes_bid, yes_ask FROM candles ORDER BY ticker, end_ts"):
        candles[r["ticker"]].append(r)
    out: dict = {}
    for s in sorted({r["series"] for r in rows}):
        ms = [r for r in rows if r["series"] == s]
        c = defaultdict(int)
        problems: list[str] = []
        prev = None
        for m in ms:
            d = json.loads(m["data"] or "{}")
            if m["close_ts"] - m["open_ts"] != 900:
                c["window_not_900s"] += 1
            if m["close_ts"] % 900:
                c["close_not_on_quarter_hour"] += 1
            t = _TICKER_TIME.search(m["ticker"])
            if t:
                yy, mon, dd, hh, mi = t.groups()
                local = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mi), tzinfo=et)
                if local.timestamp() != m["close_ts"]:
                    c["ticker_time_mismatch"] += 1
                    problems.append(f"{m['ticker']} ticker time != close_time")
            else:
                c["ticker_time_unparsed"] += 1
            ev, k = num(d.get("expiration_value")), m["floor_strike"]
            if k is None and d.get("strike_type") is None and prev is not None \
                    and m["close_ts"] - prev["close_ts"] == 900:
                # Strike shown as TBD; the rules define it as the previous window's 60 s average.
                k = num(json.loads(prev["data"] or "{}").get("expiration_value"))
                d["strike_type"] = "greater_or_equal"
                c["strike_taken_from_prev_expiration"] += 1
            if ev is None or k is None or d.get("strike_type") != "greater_or_equal":
                c["result_not_checkable"] += 1
            elif ("yes" if ev >= k else "no") != m["result"]:
                c["result_mismatch"] += 1
                problems.append(f"{m['ticker']} result {m['result']} but exp {ev} vs strike {k}")
            else:
                c["result_matches_rule"] += 1
            st = d.get("settlement_ts")
            if st and datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp() < m["close_ts"]:
                c["settled_before_close"] += 1
            if prev is not None:
                gap = m["close_ts"] - prev["close_ts"]
                if gap > 900:
                    c["missing_windows"] += int(gap // 900) - 1
                elif m["close_ts"] == prev["close_ts"]:
                    c["duplicate_close_time"] += 1
                pev = num(json.loads(prev["data"] or "{}").get("expiration_value"))
                if gap == 900 and pev is not None and m["floor_strike"] is not None:
                    c["strike_eq_prev_expiration" if abs(pev - m["floor_strike"]) < 1e-9 else "strike_ne_prev_expiration"] += 1
            prev = m
            cs = candles.get(m["ticker"], [])
            if not cs:
                c["markets_without_candles"] += 1
            inside = [x for x in cs if m["open_ts"] < x["end_ts"] < m["close_ts"]]
            c["candles_inside_window"] += len(inside)
            for x in cs:
                if x["end_ts"] % 60:
                    c["candle_not_minute_aligned"] += 1
            for x in inside:
                b, a = x["yes_bid"], x["yes_ask"]
                if b is None or a is None or not (0 < b < 1) or not (0 < a < 1):
                    c["inside_candles_one_sided"] += 1
                elif b > a:
                    c["inside_candles_crossed"] += 1
        out[s] = {
            "markets": len(ms), "yes": sum(m["result"] == "yes" for m in ms),
            "first_open_utc": datetime.fromtimestamp(ms[0]["open_ts"], timezone.utc).isoformat(),
            "last_close_utc": datetime.fromtimestamp(ms[-1]["close_ts"], timezone.utc).isoformat(),
            "expected_windows": int((ms[-1]["close_ts"] - ms[0]["close_ts"]) // 900) + 1,
            "checks": dict(c), "problem_examples": problems[:10],
        }
    return out

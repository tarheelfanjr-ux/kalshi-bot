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
import logging
import time
from datetime import datetime, timezone

import httpx

from .backtest import Candle, MarketData
from .client import PublicClient
from .db import DB
from .discovery import MarketInfo, is_short_window, num

log = logging.getLogger(__name__)


def _candle_row(ticker: str, c: dict) -> tuple:
    yb, ya = c.get("yes_bid") or {}, c.get("yes_ask") or {}
    pr = c.get("price") or {}
    return (ticker, int(c["end_period_ts"]), num(yb.get("close_dollars")), num(ya.get("close_dollars")),
            num(yb.get("low_dollars")), num(ya.get("high_dollars")), num(pr.get("close_dollars")),
            num(c.get("volume_fp")), num(c.get("open_interest_fp")))


async def collect_history(client: PublicClient, db: DB, series: list[str], max_markets: int = 5000,
                          since_ts: float = 0) -> dict[str, int]:
    """Download settled short-window markets and their minute candles. Resumable:
    markets whose candles are already stored are skipped."""
    cutoff = await client.historical_cutoff()
    settled_cut = cutoff.get("market_settled_ts")
    settled_cut = (datetime.fromisoformat(settled_cut.replace("Z", "+00:00")).timestamp()
                   if isinstance(settled_cut, str) else float(settled_cut or 0))
    counts = {}
    for s in series:
        meta = await client.series(s) or {}
        seen = 0
        sources = [(False, client.markets(series_ticker=s, status="settled")),
                   (True, client.historical_markets(series_ticker=s))]
        for historical, it in sources:
            async for m in it:
                info = MarketInfo.from_api(m, s, meta.get("fee_type", "quadratic"),
                                           float(meta.get("fee_multiplier") or 1.0))
                if not is_short_window(info) or info.result not in ("yes", "no") or info.close_ts < since_ts:
                    continue
                db.x("INSERT INTO markets(ticker,series,event_ticker,open_ts,close_ts,floor_strike,status,result,"
                     "fee_type,fee_multiplier,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(ticker) DO UPDATE SET status=excluded.status, result=excluded.result",
                     (info.ticker, s, info.event_ticker, info.open_ts, info.close_ts, info.floor_strike,
                      info.status, info.result, info.fee_type, info.fee_multiplier, time.time()))
                if db.one("SELECT 1 FROM candles WHERE ticker=? LIMIT 1", (info.ticker,)):
                    seen += 1
                    continue
                use_hist = historical or info.close_ts < settled_cut
                candles = await client.candlesticks(s, info.ticker, int(info.open_ts) - 60,
                                                    int(info.close_ts) + 60, historical=use_hist)
                with db.tx():
                    for c in candles:
                        db.x("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?)", _candle_row(info.ticker, c))
                seen += 1
                if seen >= max_markets:
                    break
            if seen >= max_markets:
                break
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

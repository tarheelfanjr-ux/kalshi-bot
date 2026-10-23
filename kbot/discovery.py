"""Find tradable ~15-minute markets from the live API. Nothing is assumed to exist."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from .client import PublicClient
from .db import DB

log = logging.getLogger(__name__)

WINDOW_MIN_S, WINDOW_MAX_S = 10 * 60, 20 * 60
TRADABLE_STATUS = {"active", "open"}   # `open` is the filter name, `active` the object status


def ts(iso: str | None) -> float | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def num(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class MarketInfo:
    ticker: str
    series: str
    event_ticker: str
    open_ts: float
    close_ts: float
    status: str
    floor_strike: float | None
    strike_type: str | None
    result: str
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    rules: str = ""

    @property
    def window_s(self) -> float:
        return self.close_ts - self.open_ts

    def seconds_left(self, now: float | None = None) -> float:
        return self.close_ts - (now if now is not None else time.time())

    @classmethod
    def from_api(cls, m: dict, series: str, fee_type="quadratic", fee_multiplier=1.0):
        return cls(
            ticker=m["ticker"], series=series, event_ticker=m.get("event_ticker", ""),
            open_ts=ts(m.get("open_time")) or 0.0, close_ts=ts(m.get("close_time")) or 0.0,
            status=m.get("status", ""), floor_strike=num(m.get("floor_strike")),
            strike_type=m.get("strike_type"), result=m.get("result") or "",
            fee_type=fee_type, fee_multiplier=fee_multiplier, rules=m.get("rules_primary", ""),
        )


def is_short_window(m: MarketInfo) -> bool:
    return WINDOW_MIN_S <= m.window_s <= WINDOW_MAX_S


def eligible(m: MarketInfo, now: float, min_left: float, max_left: float) -> tuple[bool, str]:
    if m.status not in TRADABLE_STATUS:
        return False, f"status {m.status}"
    if not is_short_window(m):
        return False, f"window {m.window_s:.0f}s"
    left = m.seconds_left(now)
    if left < min_left:
        return False, f"{left:.0f}s left < {min_left}"
    if left > max_left:
        return False, f"{left:.0f}s left > {max_left}"
    if m.floor_strike is None:
        return False, "no floor_strike"
    return True, "ok"


class Discovery:
    def __init__(self, client: PublicClient, db: DB, series: list[str] | None = None):
        self.client, self.db = client, db
        self.series_filter = series or []
        self.series_meta: dict[str, dict] = {}
        self.markets: dict[str, MarketInfo] = {}
        self.last_refresh = 0.0

    async def find_series(self) -> list[str]:
        if self.series_filter:
            names = self.series_filter
        else:
            names = sorted(s["ticker"] for s in await self.client.series_list()
                           if s.get("ticker", "").upper().endswith("15M"))
        for name in names:
            if name not in self.series_meta:
                self.series_meta[name] = await self.client.series(name) or {}
        return names

    async def refresh(self) -> dict[str, MarketInfo]:
        now = time.time()
        found: dict[str, MarketInfo] = {}
        for name in await self.find_series():
            meta = self.series_meta.get(name, {})
            async for m in self.client.markets(series_ticker=name, status="open"):
                info = MarketInfo.from_api(m, name, meta.get("fee_type", "quadratic"),
                                           float(meta.get("fee_multiplier") or 1.0))
                if is_short_window(info):
                    found[info.ticker] = info
                    self.save(info, m)
        for t, old in self.markets.items():
            if t not in found:
                self.db.event("market_left_open_set", ticker=t, close_ts=old.close_ts)
        self.markets = found
        self.last_refresh = now
        log.info("discovery: %d short-window markets across %d series", len(found), len(self.series_meta))
        return found

    def save(self, info: MarketInfo, raw: dict | None = None) -> None:
        self.db.x(
            "INSERT INTO markets(ticker,series,event_ticker,open_ts,close_ts,floor_strike,status,result,"
            "fee_type,fee_multiplier,updated_at,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET status=excluded.status, result=excluded.result, "
            "close_ts=excluded.close_ts, floor_strike=COALESCE(excluded.floor_strike, markets.floor_strike), "
            "updated_at=excluded.updated_at",
            (info.ticker, info.series, info.event_ticker, info.open_ts, info.close_ts, info.floor_strike,
             info.status, info.result, info.fee_type, info.fee_multiplier, time.time(),
             None if raw is None else json.dumps(raw)))

    async def check_result(self, ticker: str) -> MarketInfo | None:
        m = await self.client.market(ticker)
        if not m:
            return None
        series = ticker.split("-")[0]
        info = MarketInfo.from_api(m, series)
        self.save(info, m)
        return info

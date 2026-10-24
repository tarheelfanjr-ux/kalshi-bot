"""Prospective recorder for the experiments that start after 2026-10-23 (docs/EXPERIMENTS.md).

Records what cannot be downloaded later, stamped with local receive time:
  * order books of the open markets in RECORD_SERIES every `book_every_s` (batch REST, no key needed;
    WebSocket books instead when a read-only production key is configured)
  * CF Benchmarks RTI ticks from the Kalshi WebSocket (only with that key)
  * the Coinbase ticker once a second, E1's comparison feed
Settled markets, minute candles and trades can be downloaded at any time. The recorder fetches
them every 15 minutes anyway, so `kbot backfill` only has to fill gaps.

Read-only: nothing here can place or cancel an order.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

import httpx

from .auth import Signer
from .book import OrderBook
from .client import PublicClient, make_public_client
from .collector import collect_history, collect_trades
from .config import ENVIRONMENTS, Settings
from .db import DB
from .discovery import Discovery, MarketInfo, ts
from .experiments import PROSPECTIVE_START, SERIES
from .feed import UNDERLYING, CoinbaseSpot, SpotStore, WsFeed

log = logging.getLogger(__name__)

# Recorded for candidate experiments that are not frozen yet: Pyth-settled series with wider spreads,
# and new wide-spread series (docs/EXPERIMENTS.md). E1 and E2 read only SERIES, so these never reach them.
CANDIDATE_SERIES = ("KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXEURUSD15M", "KXGBPUSD15M",
                    "KXUSDJPY15M", "KXUSDCAD15M", "KXAUDUSD15M", "KXBRENT15M", "KX2YRRATE15M",
                    "KX5YRRATE15M", "KX10YRRATE15M", "KX30YRRATE15M")
RECORD_SERIES = SERIES + CANDIDATE_SERIES


async def backfill(client: PublicClient, db: DB, since_ts: float) -> int:
    """Settled markets, minute candles and trades for every recorded series. Resumable."""
    await collect_history(client, db, list(RECORD_SERIES), max_markets=10**6, since_ts=since_ts)
    return await collect_trades(client, db, since_ts)


class Recorder:
    def __init__(self, s: Settings, db: DB, book_every_s: float = 2.0, public: PublicClient | None = None,
                 slow: PublicClient | None = None, http: httpx.AsyncClient | None = None):
        self.s, self.db, self.book_every_s = s, db, book_every_s
        # Kalshi throttles keyless reads near 3.5 req/s per IP, so books get their own budget
        # (1 req/s) and backfill paging gets the rest (1.5 req/s).
        self.public = public or make_public_client(s, read_tokens_per_s=10)
        self.slow = slow or make_public_client(s, read_tokens_per_s=15, max_retries=8)
        self.http = http or httpx.AsyncClient(timeout=5)
        # Discovery and fee checks page through 17 series, so they use the slow budget and run in the
        # background. On the book budget they stalled snapshots for 20 to 50 s at a time.
        self.discovery = Discovery(self.slow, db, list(RECORD_SERIES))
        self.books: dict[str, OrderBook] = {}
        self.ws = None
        if s.data_key_id and s.data_private_key_path:
            self.ws = WsFeed(ENVIRONMENTS["production"]["ws"],
                             Signer.from_file(s.data_key_id, s.data_private_key_path),
                             self.books, SpotStore(), on_index=self.on_index)
        self._last: dict[str, tuple[float, tuple]] = {}
        self._timers: dict[str, float] = {}
        self._stop = asyncio.Event()

    def _every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._timers.get(name, 0) >= seconds:
            self._timers[name] = now
            return True
        return False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # Windows
                pass
        self.db.interruption("recorder", "recorder_heartbeat")
        self.db.event("recorder_start", rti=bool(self.ws), book_every_s=self.book_every_s)
        self.db.set("recorder_heartbeat", time.time())     # a crash before the first tick still shows as downtime
        tasks = [asyncio.create_task(self._backfill_loop()), asyncio.create_task(self._discovery_loop())]
        if self.ws:
            tasks.append(asyncio.create_task(self.ws.run()))
        try:
            while not self._stop.is_set():
                try:
                    await self.tick(time.time())
                except Exception as e:
                    log.exception("recorder tick failed")
                    self.db.event("recorder_error", error=repr(e)[:300])
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1 - time.time() % 1)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self.ws:
                self.ws.stop()
            for t in tasks:
                t.cancel()
            self.db.event("recorder_stop")

    async def tick(self, now: float) -> None:
        markets = [m for m in self.discovery.markets.values() if m.open_ts <= now < m.close_ts]
        if self._every("books", self.book_every_s - 0.5, now):   # ticks land on whole seconds, give or take
            if self.ws:
                self.ws.set_markets({m.ticker for m in markets},
                                    {UNDERLYING[m.series][0] for m in markets if m.series in UNDERLYING})
            elif markets:
                got = await self.public.orderbooks(sorted(m.ticker for m in markets))
                recv = time.time()
                for t, ob in got.items():
                    self.books[t] = OrderBook.from_rest(t, ob, recv)
            with self.db.tx():
                for m in markets:
                    self.snapshot(m, time.time())
        await self.poll_coinbase(markets)
        if self._every("health", 60, now):
            self.db.event("recorder_health", clock_skew_s=self.public.clock_skew_s,
                          ws=bool(self.ws and self.ws.connected), markets=len(markets))
        self.db.set("recorder_heartbeat", now)

    def snapshot(self, m: MarketInfo, now: float) -> None:
        """Top of book, one row per market per interval. `ts` is when the state was known locally,
        never earlier. Unchanged books are written at most every 10 s so gaps stay visible."""
        book = self.books.get(m.ticker)
        if book is None or book.age(now) > 5:
            return
        yb, ya = book.best_bid("yes"), book.best_ask("yes")
        top = (yb, ya)
        last = self._last.get(m.ticker)
        if last and last[1] == top and now - last[0] < 10:
            return
        self._last[m.ticker] = (now, top)
        self.db.x("INSERT INTO snapshots(ts,ticker,yes_bid,yes_ask,yes_bid_size,yes_ask_size,seconds_left) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (now, m.ticker, yb[0] if yb else None, ya[0] if ya else None, yb[1] if yb else None,
                   ya[1] if ya else None, m.close_ts - now))

    async def poll_coinbase(self, markets: list[MarketInfo]) -> None:
        products = sorted({UNDERLYING[m.series][1] for m in markets if m.series in UNDERLYING})

        async def one(product):
            try:
                r = await self.http.get(CoinbaseSpot.URL.format(product))
                r.raise_for_status()
                return product, time.time(), r.json()
            except Exception as e:
                log.warning("coinbase %s failed: %s", product, e)
                return None
        for got in await asyncio.gather(*(one(p) for p in products)):
            if got:
                product, recv, d = got
                self.db.x("INSERT OR IGNORE INTO index_ticks(index_id,source,recv_ts,src_ts,value) VALUES(?,?,?,?,?)",
                          (product, "coinbase", recv, ts(d.get("time")), float(d["price"])))

    def on_index(self, msg: dict, data: dict, recv_ts: float) -> None:
        if "value" not in data:
            return
        src = data.get("time") or msg.get("received_at")
        final = (msg.get("last_60s_windowed_average_15min") or {}).get("value")
        self.db.x("INSERT OR IGNORE INTO index_ticks VALUES(?,?,?,?,?,?,?)",
                  (msg["index_id"], "cfbenchmarks", recv_ts, float(src) / 1000 if src else None,
                   float(data["value"]), float((msg.get("avg_60s_data") or {}).get("value") or 0) or None,
                   float(final) if final else None))

    async def log_fees(self) -> None:
        """E2 and the candidates assume a zero maker fee, so any change to the fee type is recorded hourly."""
        series = {s: await self.slow.series(s) or {} for s in RECORD_SERIES}
        overrides = [o async for o in self.slow.paginate("/events/fee_changes", "event_fee_changes")
                     if o.get("series_ticker") in RECORD_SERIES]
        self.db.event("fee_state", series={s: [m.get("fee_type"), m.get("fee_multiplier")] for s, m in series.items()},
                      scheduled=await self.public.fee_changes(), event_overrides=overrides)

    async def _discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.discovery.refresh()      # /markets is cached 15 s; new windows show up within ~35 s
                if self._every("fees", 3600, time.time()):
                    await self.log_fees()
            except Exception as e:
                log.exception("discovery failed")
                self.db.event("recorder_error", error=f"discovery: {e!r}"[:300])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    async def _backfill_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await backfill(self.slow, self.db, max(PROSPECTIVE_START, time.time() - 86400))
            except Exception as e:
                log.exception("backfill failed")
                self.db.event("recorder_error", error=f"backfill: {e!r}"[:300])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=900)
            except asyncio.TimeoutError:
                pass

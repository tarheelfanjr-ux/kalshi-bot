"""Live market data: order books and the underlying index price.

Two book sources:
  RestBookPoller  unauthenticated REST polling, used when no credentials are configured.
  WsFeed          authenticated WebSocket (orderbook_delta, cfbenchmarks_value,
                  market_lifecycle_v2) with automatic reconnect and seq-gap resync.

Two spot sources:
  WsFeed cfbenchmarks_value  the actual settlement index family (CF Benchmarks RTI).
  CoinbaseSpot               public Coinbase ticker, a proxy for the index. Labelled as such.

Every value carries a local receive timestamp; consumers reject stale data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque

import httpx

from .auth import Signer
from .book import OrderBook
from .client import PublicClient
from .db import DB

log = logging.getLogger(__name__)

# Series -> (CF Benchmarks index id, Coinbase product). Only these get a fair-value model.
UNDERLYING = {
    "KXBTC15M": ("BRTI", "BTC-USD"),
    "KXETH15M": ("ETHUSD_RTI", "ETH-USD"),
    "KXSOL15M": ("SOLUSD_RTI", "SOL-USD"),
    "KXXRP15M": ("XRPUSD_RTI", "XRP-USD"),
    "KXDOGE15M": ("DOGEUSD_RTI", "DOGE-USD"),
}


class SpotStore:
    """Rolling (ts, value) history per index, plus the exchange's own 60 s average."""

    def __init__(self, db: DB | None = None, keep_s: float = 3600):
        self.hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=20000))
        self.avg60: dict[str, tuple[float, float]] = {}
        self.db, self.keep_s = db, keep_s
        self.source: dict[str, str] = {}

    def add(self, index_id: str, ts: float, value: float, source: str) -> None:
        h = self.hist[index_id]
        if h and ts <= h[-1][0]:
            return
        h.append((ts, value))
        self.source[index_id] = source
        if self.db is not None:
            self.db.x("INSERT OR IGNORE INTO spot(index_id,ts,value) VALUES(?,?,?)", (index_id, ts, value))

    def latest(self, index_id: str) -> tuple[float, float] | None:
        h = self.hist.get(index_id)
        return h[-1] if h else None

    def points(self, index_id: str) -> list[tuple[float, float]]:
        return list(self.hist.get(index_id, ()))


class RestBookPoller:
    def __init__(self, client: PublicClient, books: dict[str, OrderBook]):
        self.client, self.books = client, books
        self.last_ok = 0.0

    async def poll(self, tickers: list[str]) -> None:
        for i in range(0, len(tickers), 100):          # one request per 100 books
            try:
                got = await self.client.orderbooks(tickers[i:i + 100])
            except Exception as e:
                log.warning("orderbooks failed: %s", e)
                continue
            now = time.time()
            for t, ob in got.items():
                self.books[t] = OrderBook.from_rest(t, ob, now)
            self.last_ok = now


class CoinbaseSpot:
    URL = "https://api.exchange.coinbase.com/products/{}/ticker"

    def __init__(self, spot: SpotStore, http: httpx.AsyncClient | None = None):
        self.spot, self.http = spot, http or httpx.AsyncClient(timeout=5)

    async def poll(self, series: list[str]) -> None:
        for s in series:
            if s not in UNDERLYING:
                continue
            index_id, product = UNDERLYING[s]
            try:
                r = await self.http.get(self.URL.format(product))
                r.raise_for_status()
                self.spot.add(index_id, time.time(), float(r.json()["price"]), "coinbase_proxy")
            except Exception as e:
                log.warning("coinbase %s failed: %s", product, e)


class WsFeed:
    """Authenticated WebSocket feed with reconnect. Books are rebuilt from a fresh
    snapshot after any reconnect or sequence gap, so a missed delta can't corrupt them."""

    def __init__(self, url: str, signer: Signer, books: dict[str, OrderBook], spot: SpotStore,
                 on_lifecycle=None, on_index=None):
        self.url, self.signer, self.books, self.spot = url, signer, books, spot
        self.on_lifecycle = on_lifecycle or (lambda msg: None)
        self.on_index = on_index or (lambda msg, data, recv_ts: None)
        self.tickers: set[str] = set()
        self.index_ids: set[str] = set()
        self.connected = False
        self.last_msg = 0.0
        self.reconnects = 0
        self._ws = None
        self._seq: dict[int, int] = {}
        self._id = 0
        self._stop = asyncio.Event()
        self._resub = asyncio.Event()

    def set_markets(self, tickers: set[str], index_ids: set[str]) -> None:
        if tickers != self.tickers or index_ids != self.index_ids:
            self.tickers, self.index_ids = set(tickers), set(index_ids)
            self._resub.set()

    async def _send(self, cmd: dict) -> None:
        self._id += 1
        cmd["id"] = self._id
        await self._ws.send(json.dumps(cmd))

    async def _subscribe(self) -> None:
        self._seq.clear()
        if self.tickers:
            await self._send({"cmd": "subscribe", "params": {
                "channels": ["orderbook_delta"], "market_tickers": sorted(self.tickers)}})
        await self._send({"cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}})
        if self.index_ids:
            await self._send({"cmd": "subscribe", "params": {
                "channels": ["cfbenchmarks_value"], "index_ids": sorted(self.index_ids)}})

    def handle(self, raw: str) -> bool:
        """Process one message. Returns False when a resync is needed."""
        now = time.time()
        self.last_msg = now
        m = json.loads(raw)
        typ, msg, sid, seq = m.get("type"), m.get("msg") or {}, m.get("sid"), m.get("seq")
        if sid is not None and seq is not None:
            prev = self._seq.get(sid)
            self._seq[sid] = seq
            if prev is not None and seq != prev + 1:
                log.warning("seq gap on sid %s: %s -> %s", sid, prev, seq)
                return False
        if typ == "orderbook_snapshot":
            t = msg["market_ticker"]
            book = self.books.get(t) or OrderBook(t)
            book.apply_snapshot(msg, now)
            self.books[t] = book
        elif typ == "orderbook_delta":
            book = self.books.get(msg["market_ticker"])
            if book is None:
                return False
            book.apply_delta(msg, now)
        elif typ == "cfbenchmarks_value":
            data = json.loads(msg["data"]) if isinstance(msg.get("data"), str) else msg.get("data") or {}
            if "value" in data:
                ts = float(data.get("time", msg.get("received_at", now * 1000))) / 1000
                self.spot.add(msg["index_id"], ts, float(data["value"]), "cfbenchmarks")
            avg = msg.get("avg_60s_data") or {}
            if avg.get("value"):
                self.spot.avg60[msg["index_id"]] = (now, float(avg["value"]))
            self.on_index(msg, data, now)
        elif typ == "market_lifecycle_v2":
            self.on_lifecycle(msg)
        elif typ == "error":
            log.error("ws error: %s", msg)
        return True

    async def run(self) -> None:
        import websockets  # imported here so paper mode without websockets still imports

        backoff = 1
        while not self._stop.is_set():
            try:
                headers = self.signer.headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(self.url, additional_headers=headers,
                                              ping_interval=10, ping_timeout=20) as ws:
                    self._ws, self.connected, backoff = ws, True, 1
                    await self._subscribe()
                    self._resub.clear()
                    while not self._stop.is_set():
                        if self._resub.is_set():
                            break           # reconnect cleanly with the new subscription set
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            log.warning("ws: no message in 30s, reconnecting")
                            break
                        if not self.handle(raw):
                            break
            except Exception as e:
                log.warning("ws disconnected: %r", e)
            self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            self.books.clear()          # never trade on books from a dead connection
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stop.set()

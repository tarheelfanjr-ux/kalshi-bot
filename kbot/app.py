"""Long-running trading loop for paper, demo, and production modes.

One asyncio process. Order of work each tick:
  health checks -> settle finished markets -> evaluate eligible markets -> risk -> execute.
Discovery, reconciliation, equity snapshots, and backups run on their own timers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time

from .book import OrderBook
from .client import AccountClient, make_public_client, make_signer, make_trading_client
from .config import ENVIRONMENTS, Settings
from .db import DB
from .discovery import Discovery, eligible
from .execution import KalshiAdapter, OrderManager, PaperAdapter
from .experiments import SERIES
from .feed import UNDERLYING, CoinbaseSpot, RestBookPoller, SpotStore, WsFeed
from .maker import SHADOW_REASON, MakerEngine
from .model import realized_sigma
from .notify import Notifier
from .risk import RiskManager
from .strategy import Candidate, Snapshot, make

log = logging.getLogger(__name__)


class App:
    def __init__(self, s: Settings, db: DB | None = None):
        if s.mode not in ("paper", "demo", "production"):
            raise ValueError("the live app runs in paper, demo, or production mode")
        maker = s.strategy.name == "maker"
        if maker and s.mode != "paper":
            raise ValueError("the maker strategy runs in paper mode only. Only its owner lifts this (docs/GO_LIVE.md).")
        self.s = s
        self.db = db or DB(s.db_path)
        self.notify = Notifier(self.db, s.notify_webhook)
        self.public = make_public_client(s)
        self.books: dict[str, OrderBook] = {}
        self.spot = SpotStore(self.db)
        self.mid_hist: dict[str, list[tuple[float, float]]] = {}
        self.discovery = Discovery(self.public, self.db, s.series or (list(SERIES) if maker else None))
        # In maker mode fair_value runs beside it as a shadow benchmark: logged, never traded.
        self.strategy = make("fair_value" if maker else s.strategy.name, s.strategy)
        self.signer = None
        if s.api_key_id and s.private_key_path:
            self.signer = make_signer(s)
        # WebSocket data needs a key for the *data* environment. Paper mode on production data
        # can use an optional read-only production key; it is never given to a trading client.
        data_signer = self.signer if s.data_env == s.trade_env else None
        if s.mode == "paper" and s.data_env == "production" and s.data_key_id and s.data_private_key_path:
            from .auth import Signer
            data_signer = Signer.from_file(s.data_key_id, s.data_private_key_path)
        if s.mode == "paper":
            adapter = PaperAdapter(self.books.get, max_book_age_s=s.max_data_age_s)
            self.account = None
        else:
            trading = make_trading_client(s, self.signer)
            adapter = KalshiAdapter(trading)
            self.account: AccountClient | None = trading
        self.om = OrderManager(self.db, adapter, self.notify)
        self.risk = RiskManager(self.db, s.risk, s.mode)
        self.maker = MakerEngine(self.db, self.discovery, self.om, self.risk, self.books, adapter) if maker else None
        self._shadowed = {r["ticker"] for r in self.db.q(
            "SELECT DISTINCT ticker FROM signals WHERE strategy='fair_value' AND reason=?", (SHADOW_REASON,))}
        self.ws = None
        if data_signer:
            self.ws = WsFeed(ENVIRONMENTS[s.data_env]["ws"], data_signer, self.books, self.spot,
                             on_lifecycle=self._on_lifecycle)
        self.poller = RestBookPoller(self.public, self.books)
        self.coinbase = None if self.ws else CoinbaseSpot(self.spot)
        self.health_reason = "starting"
        self.unhealthy_since: float | None = time.time()
        self._stop = asyncio.Event()
        self._timers: dict[str, float] = {}

    # --- lifecycle --------------------------------------------------------------
    def startup_checks(self) -> None:
        if not self.db.integrity_ok():
            raise RuntimeError("database integrity check failed; restore from data/backups")
        prev = self.db.get("running")
        if prev and prev.get("pid"):
            self.db.event("unexpected_termination", self.s.mode, last_heartbeat=self.db.get("heartbeat"))
            self.notify(f"previous run (pid {prev['pid']}) did not shut down cleanly; reconciling before trading")
        self.db.set("running", {"pid": os.getpid(), "mode": self.s.mode, "started": time.time()})
        self.db.event("startup", self.s.mode, strategy=self.s.strategy.name, bankroll=self.s.risk.bankroll,
                      production_armed=self.s.production_armed())

    async def run(self) -> None:
        self.startup_checks()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # Windows
                pass
        if self.s.mode != "paper":
            await self.reconcile()          # restore true state before any new order
        tasks = [asyncio.create_task(self.ws.run())] if self.ws else []
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception as e:
                    log.exception("tick failed")
                    self.health_reason = f"tick error: {e!r}"[:200]
                    self.db.event("error", self.s.mode, error=repr(e)[:500])
                    self.db.set("health", {"ok": False, "reason": self.health_reason, "ts": time.time()})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.loop_interval_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks) -> None:
        log.info("shutting down")
        if self.maker:      # resting bids must not outlive the process that manages them
            try:
                await self.om.cancel_open_entries("shutdown")
            except Exception as e:
                log.error("cancel on shutdown failed: %s. Check open orders on Kalshi.", e)
                self.notify(f"could not cancel resting orders on shutdown: {e!r}"[:300])
        if self.ws:
            self.ws.stop()
        for t in tasks:
            t.cancel()
        self.db.set("running", None)
        self.db.event("shutdown", self.s.mode, open_positions=len(self.db.q(
            "SELECT 1 FROM positions WHERE mode=? AND status='open'", (self.s.mode,))))
        # Positions are NOT closed on shutdown. They settle on the exchange as usual.

    def stop(self) -> None:
        self._stop.set()

    def _every(self, name: str, seconds: float) -> bool:
        now = time.time()
        if now - self._timers.get(name, 0) >= seconds:
            self._timers[name] = now
            return True
        return False

    def _on_lifecycle(self, msg: dict) -> None:
        self.db.event("lifecycle", ticker=msg.get("market_ticker"), data=msg)

    # --- health -----------------------------------------------------------------
    def check_health(self, active: list[str]) -> tuple[bool, str]:
        now = time.time()
        problems = []
        if self.db.get("estop"):
            problems.append("emergency stop engaged")
        if self.ws and not self.ws.connected:
            problems.append("websocket disconnected")
        skew = self.public.clock_skew_s
        if skew is not None and abs(skew) > self.s.max_clock_skew_s:
            problems.append(f"clock skew {skew:+.1f}s")
        if not self.public.last_ok or now - self.public.last_ok > 60:
            problems.append("no successful API call in 60s")
        stale = [t for t in active if t not in self.books or self.books[t].age(now) > self.s.max_data_age_s]
        if active and len(stale) == len(active):
            problems.append(f"all {len(active)} books stale")
        if self.s.mode != "paper":
            last = self.db.get(f"last_reconcile:{self.s.mode}", 0)
            if now - last > 120:
                problems.append("reconciliation older than 120s")
        ok = not problems
        reason = "; ".join(problems) or "ok"
        if ok:
            self.unhealthy_since = None
        else:
            self.unhealthy_since = self.unhealthy_since or now
            if now - self.unhealthy_since > 300 and self._every("unhealthy_notify", 900):
                self.notify(f"unhealthy for {now - self.unhealthy_since:.0f}s: {reason}")
        self.health_reason = reason
        self.db.set("health", {"ok": ok, "reason": reason, "ts": now, "ws": bool(self.ws and self.ws.connected),
                               "clock_skew_s": skew, "last_api_ok": self.public.last_ok})
        return ok, reason

    # --- main tick ----------------------------------------------------------------
    async def tick(self) -> None:
        now = time.time()
        self.db.set("heartbeat", now)
        if self._every("discovery", 60):
            await self.discovery.refresh()
        markets = {t: m for t, m in self.discovery.markets.items() if m.close_ts > now}
        active = sorted(markets)
        index_ids = {UNDERLYING[m.series][0] for m in markets.values() if m.series in UNDERLYING}
        if self.ws:
            self.ws.set_markets(set(active), index_ids)
        else:
            await self.poller.poll(active)
            await self.coinbase.poll(sorted({m.series for m in markets.values()}))
        if self.s.mode != "paper" and self._every("reconcile", 30):
            await self.reconcile()
        healthy, reason = self.check_health(active)
        await self.settle_finished()
        if (e := self.db.get("estop")) and not self.db.get("estop_handled"):
            n = await self.om.cancel_open_entries("emergency stop")
            self.db.set("estop_handled", True)
            self.notify(f"EMERGENCY STOP: entries blocked, {n} open entry orders cancelled")
        if (t := self.risk.check_limits()) and not self.risk.halted():
            self.risk.trip(t)
            await self.om.cancel_open_entries(t)
            self.notify(f"risk limit hit, entries halted until reviewed: {t}")
        if self.maker:
            await self.maker.tick(markets, now, healthy, reason)
        for t in active:
            await self.evaluate(markets[t], now, healthy, reason)
        if self._every("equity", 60):
            self.record_equity()
        if self.account and self._every("balance", 60):
            b = await self.account.balance()
            self.db.set(f"balance:{self.s.mode}", b.get("balance_dollars"))
        if self._every("backup", 86400):
            self.db.backup()

    async def evaluate(self, m, now, healthy, health_reason) -> None:
        book = self.books.get(m.ticker)
        if book is None:
            return
        mid = book.mid_yes()
        if mid is not None:
            h = self.mid_hist.setdefault(m.ticker, [])
            h.append((book.updated_at, mid))
            del h[:-600]
            if self._every(f"snap:{m.ticker}", 10):
                self._record_snapshot(m, book, now)
        ok, why = eligible(m, now, self.s.strategy.min_seconds_left, self.s.strategy.max_seconds_left)
        if not ok:
            return
        if book.age(now) > self.s.max_data_age_s:
            self._signal(None, m.ticker, f"stale book ({book.age(now):.1f}s)")
            return
        snap = Snapshot(now=now, market=m, book=book, mid_history=self.mid_hist.get(m.ticker, []))
        if m.series in UNDERLYING:
            idx = UNDERLYING[m.series][0]
            last = self.spot.latest(idx)
            if last:
                snap.spot_ts, snap.spot = last
                snap.sigma = realized_sigma(self.spot.points(idx), self.s.strategy.vol_lookback_s, now=now)
                if m.seconds_left(now) <= 60 and idx in self.spot.avg60:
                    snap.known_avg = self.spot.avg60[idx][1]
        cand = self.strategy.evaluate(snap)
        if self.maker:      # shadow benchmark: log the first fair_value signal per market, never trade it
            if isinstance(cand, Candidate) and m.ticker not in self._shadowed:
                self._shadowed.add(m.ticker)
                self._signal(cand, m.ticker, SHADOW_REASON)
            return
        if not isinstance(cand, Candidate):
            if self._every(f"rej:{m.ticker}:{cand.reason[:20]}", 30):   # throttle repeated identical rejections
                self._signal(None, m.ticker, cand.reason)
            return
        decision = self.risk.approve(cand, healthy=healthy, health_reason=health_reason)
        sid = self._signal(cand, m.ticker, decision.reason, accepted=decision.approved, count=decision.count)
        if decision.trip:
            await self.om.cancel_open_entries(decision.trip)
            self.notify(f"risk limit hit, entries halted until reviewed: {decision.trip}")
        if not decision.approved:
            return
        # Re-check the executable price immediately before sending.
        fresh = self.books.get(m.ticker)
        top = fresh.best_ask(cand.outcome_side) if fresh else None
        if top is None or top[0] > cand.max_entry_price or fresh.age() > self.s.max_data_age_s:
            self.db.event("entry_abandoned", self.s.mode, m.ticker, reason="price moved or data stale")
            return
        await self.om.place(ticker=m.ticker, outcome_side=cand.outcome_side, limit_price=cand.max_entry_price,
                            count=decision.count, signal_id=sid)

    def _signal(self, cand: Candidate | None, ticker: str, reason: str, accepted=False, count=0.0) -> int:
        if cand is None:
            cur = self.db.x("INSERT INTO signals(ts,mode,strategy,ticker,accepted,reason) VALUES(?,?,?,?,0,?)",
                            (time.time(), self.s.mode, self.s.strategy.name, ticker, reason))
        else:
            cur = self.db.x(
                "INSERT INTO signals(ts,mode,strategy,ticker,outcome_side,entry_price,q_win,market_q,net_edge,"
                "count,accepted,reason,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), self.s.mode, cand.strategy, ticker, cand.outcome_side, cand.entry_price, cand.q_win,
                 cand.market_q, cand.net_edge, count, int(accepted), reason, json.dumps(cand.to_dict())))
        return cur.lastrowid

    def _record_snapshot(self, m, book: OrderBook, now: float) -> None:
        yb, ya = book.best_bid("yes"), book.best_ask("yes")
        idx = UNDERLYING.get(m.series, (None,))[0]
        last = self.spot.latest(idx) if idx else None
        self.db.x("INSERT INTO snapshots(ts,ticker,yes_bid,yes_ask,yes_bid_size,yes_ask_size,depth,spot,spot_ts,"
                  "seconds_left) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now, m.ticker, yb[0] if yb else None, ya[0] if ya else None, yb[1] if yb else None,
                   ya[1] if ya else None, json.dumps(book.to_json()), last[1] if last else None,
                   last[0] if last else None, m.seconds_left(now)))

    # --- settlement and reconciliation ------------------------------------------
    async def settle_finished(self) -> None:
        now = time.time()
        rows = self.db.q("SELECT DISTINCT ticker FROM positions WHERE mode=? AND status='open' "
                         "AND (close_ts IS NULL OR close_ts < ?)", (self.s.mode, now - 30))
        for r in rows:
            if not self._every(f"settle:{r['ticker']}", 30):
                continue
            info = await self.discovery.check_result(r["ticker"])
            # Book only once the exchange has paid out, so local and exchange positions agree.
            if info and info.result in ("yes", "no") and info.status in ("finalized", "settled"):
                pnl = self.om.settle(r["ticker"], info.result)
                log.info("settled %s result=%s pnl=%.4f", r["ticker"], info.result, pnl)

    async def reconcile(self) -> None:
        problems = await self.om.reconcile_orders()
        problems += await self.om.reconcile_positions()
        self.db.set(f"reconcile_problems:{self.s.mode}", problems)
        if problems:
            self.risk.trip("reconciliation mismatch: " + "; ".join(problems)[:300])
            self.notify("reconciliation mismatch, entries halted: " + "; ".join(problems)[:300])

    def record_equity(self) -> None:
        realized = self.db.one("SELECT COALESCE(SUM(pnl),0) s FROM positions WHERE mode=? AND status='settled'",
                               (self.s.mode,))["s"]
        open_value = 0.0
        for p in self.db.q("SELECT * FROM positions WHERE mode=? AND status='open'", (self.s.mode,)):
            b = self.books.get(p["ticker"])
            bid = b.best_bid(p["outcome_side"]) if b else None
            open_value += p["count"] * (bid[0] if bid else 0) - p["cost"] - p["fees"]
        cash = self.s.risk.bankroll + realized
        self.db.x("INSERT INTO equity(ts,mode,cash,open_value,equity) VALUES(?,?,?,?,?)",
                  (time.time(), self.s.mode, cash, open_value, cash + open_value))

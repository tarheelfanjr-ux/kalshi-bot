"""Maker-order strategy (hypothesis H2 / experiment E2) for paper trading and a historical check.

The rule is E2's, unchanged: at each minute close from 840 s to 60 s before close, rest a
1-contract bid at the best YES bid and another at the best NO bid, each until the next minute.
Bids only from 0.05 to 0.95, at most one fill per side per market, held to settlement.

Fills are never assumed. A resting bid fills only when a public taker trade prints at a price
worse than ours on our side, at least `latency_s` after we joined (we must have been hit first).
Trades at exactly our price do not fill us, since we do not know our place in the queue.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from datetime import datetime, timezone

from .book import OrderBook
from .client import PublicClient
from .collector import collect_trades
from .db import DB
from .discovery import TRADABLE_STATUS, Discovery, MarketInfo, ts
from .execution import OrderManager, PaperAdapter
from .experiments import SERIES, SPECS, e2_report, summary
from .fees import MAKER_RATE, TAKER_RATE, fee
from .reports import performance
from .risk import OPEN_ORDER_STATES, RiskManager

log = logging.getLogger(__name__)

# The audit's opened holdout starts here (chronological_split of the 90-day set). Nothing at or
# after it is read by the historical check, and nothing from the prospective period either.
HOLDOUT_START = datetime(2026, 10, 5, 14, 15, tzinfo=timezone.utc).timestamp()
HISTORY_SAMPLE = {"KXBTC15M": 100, "KXETH15M": 400, "KXSOL15M": 400, "KXXRP15M": 400}
HISTORY_LABEL = ("HISTORICAL BACKTEST (minute-candle quotes, public trades, trade-through fills only; "
                 "random sample of markets that closed before the audit holdout)")


def sample_markets(db: DB, per_series: dict[str, int] = HISTORY_SAMPLE, seed: int = 7) -> list[str]:
    """Seeded random sample, fixed before any result is seen. No market from the holdout."""
    rng = random.Random(seed)
    out = []
    for s in SERIES:
        tickers = [r["ticker"] for r in db.q("SELECT ticker FROM markets WHERE series=? AND result IN ('yes','no') "
                                             "AND close_ts < ? ORDER BY close_ts", (s, HOLDOUT_START))]
        out += rng.sample(tickers, min(per_series.get(s, 0), len(tickers)))
    return out


async def history(client: PublicClient | None, db: DB, per_series: dict[str, int] = HISTORY_SAMPLE,
                  seed: int = 7, level: float = 0.95) -> dict:
    """E2's scoring on downloadable history. Downloads trades for the sample first (resumable)."""
    tickers = sample_markets(db, per_series, seed)
    if client is not None:
        await collect_trades(client, db, 0, tickers=tickers)
    have = {r["ticker"] for r in db.q("SELECT DISTINCT ticker FROM trades")} & set(tickers)
    if not have:
        return {"label": HISTORY_LABEL, "markets": 0, "why": "no trades downloaded yet"}
    first = db.one(f"SELECT MIN(open_ts) t FROM markets WHERE ticker IN ({','.join('?' * len(have))})",
                   tuple(have))["t"]
    rep = e2_report(db, first, HOLDOUT_START, level, floor=0, tickers=have)
    return {"label": HISTORY_LABEL, "sample": {"seed": seed, "per_series": per_series, "requested": len(tickers),
                                               "with_trades": len(have)}, **rep}


# --- live paper trading ---------------------------------------------------------------------

SHADOW_REASON = "shadow: fair_value benchmark, not traded"
PAPER_LABEL = ("SHORT PROSPECTIVE PAPER TEST (live Kalshi data, simulated fills, no orders sent). "
               "Not E2's frozen evaluation. A small sample cannot show an edge: read the CI, not the mean.")
LIVE_LABEL = ("SHORT PROSPECTIVE TEST WITH REAL ORDERS ON {mode} (account fills come from the exchange). "
              "Not E2's frozen evaluation. A small sample cannot show an edge: read the CI, not the mean.")


class MakerEngine:
    """E2's quoting rule, live.

    Every quote the rule wants goes in `quotes`, the 1-contract research log, whether or not the
    risk manager lets the account place an order for it. The log always gets its fills from the
    public trade tape by the trade-through rule.

    Paper mode: the paper order fills when its quote does, and is cancelled once the tape has been
    read past the quote's end. Demo and production: the order is a real post-only bid. Its fills
    come only from the exchange (reconciliation), and it is cancelled the moment its quote ends.
    Comparing the two on the same quotes checks the paper fill model.
    """
    API_LAG_S = 5.0          # a trade can reach /markets/trades a few seconds after it prints
    TRADES_EVERY_S = 4.0
    OVERLAP_S = 15.0         # each trade poll re-reads this much; trade ids remove duplicates

    def __init__(self, db: DB, discovery: Discovery, om: OrderManager, risk: RiskManager,
                 books: dict[str, OrderBook], adapter):
        self.db, self.discovery, self.om, self.risk, self.books, self.adapter = db, discovery, om, risk, books, adapter
        self.mode = om.mode
        self.paper = isinstance(adapter, PaperAdapter)
        self.spec = SPECS["E2"]
        self._minute: dict[str, int] = {}
        self._cursor: dict[str, float] = {}
        self._polled: dict[str, float] = {}
        self._seen: dict[str, dict[str, float]] = {}
        self._timers: dict[str, float] = {}
        if not self.paper:
            return              # real orders live on the exchange; reconciliation restores them
        # Paper orders live only in memory: after a restart, keep the ones whose quote is still open.
        live = {r["client_order_id"] for r in db.q("SELECT client_order_id FROM quotes WHERE mode=? AND status='open' "
                                                   "AND client_order_id IS NOT NULL", (self.mode,))}
        for o in db.q("SELECT client_order_id, count, filled FROM orders WHERE mode=? "
                      "AND state IN ('resting','partially_filled')", (self.mode,)):
            adapter.restore(o["client_order_id"], o["count"] - o["filled"], resting=o["client_order_id"] in live)

    def _every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._timers.get(name, 0) >= seconds:
            self._timers[name] = now
            return True
        return False

    async def tick(self, markets: dict[str, MarketInfo], now: float, healthy: bool, health_reason: str) -> None:
        if self._every("trades", self.TRADES_EVERY_S, now):
            await self.poll_trades()
        await self.expire()
        if self.paper or self._every("orders", 5, now):     # paper orders follow their quotes; real ones cost API calls
            await self.om.reconcile_orders()
        minute = int(now // 60)
        for m in markets.values():
            if self._minute.get(m.ticker) != minute:
                self._minute[m.ticker] = minute
                await self.quote(m, now, healthy, health_reason)
        if not self.paper:
            await self.cancel_ended(now)
        if self._every("results", 60, now):
            await self.fetch_results(now)

    # --- quoting --------------------------------------------------------------------------
    def _fee(self, ticker: str, price: float) -> float:
        r = self.db.one("SELECT fee_type, fee_multiplier FROM markets WHERE ticker=?", (ticker,))
        return fee(price, 1, maker=True, fee_type=(r and r["fee_type"]) or "quadratic",
                   multiplier=(r and r["fee_multiplier"]) or 1.0)

    def _log(self, ticker, reason, side=None, price=None, market_q=None, accepted=False, count=0.0) -> None:
        self.db.x("INSERT INTO signals(ts,mode,strategy,ticker,outcome_side,entry_price,market_q,count,accepted,reason) "
                  "VALUES(?,?,'maker',?,?,?,?,?,?,?)",
                  (time.time(), self.mode, ticker, side, price, market_q, count, int(accepted), reason))

    def _order_live(self, coid: str | None) -> bool:
        r = coid and self.db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))
        return bool(r) and r["state"] in OPEN_ORDER_STATES

    async def _clear_side(self, ticker: str, side: str) -> bool:
        """Real orders: cancel any order still working on this side. True once none is left."""
        open_ = f"mode=? AND ticker=? AND outcome_side=? AND state IN ({','.join('?' * len(OPEN_ORDER_STATES))})"
        for o in self.db.q(f"SELECT client_order_id FROM orders WHERE {open_}", (self.mode, ticker, side, *OPEN_ORDER_STATES)):
            try:
                await self.om.cancel(o["client_order_id"])
            except Exception as e:
                log.error("cancel %s failed: %s", o["client_order_id"], e)
        return not self.db.one(f"SELECT 1 FROM orders WHERE {open_}", (self.mode, ticker, side, *OPEN_ORDER_STATES))

    async def _place(self, m: MarketInfo, side, price, healthy, health_reason) -> tuple[str | None, str]:
        # Two working bids on one side could both fill. A fill found here counts in the inventory check below.
        if not self.paper and not await self._clear_side(m.ticker, side):
            return None, "previous order on this side still working"
        d = self.risk.approve_quote(m.ticker, side, price, 1, self._fee(m.ticker, price), healthy=healthy,
                                    health_reason=health_reason)
        if not d.approved:
            return None, d.reason
        coid = await self.om.place(ticker=m.ticker, outcome_side=side, limit_price=price, count=d.count,
                                   post_only=True, expiration_ts=m.close_ts)
        state = self.db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))["state"]
        # An order whose create timed out may still be resting: keep it on the quote so it gets cancelled.
        return (coid, d.reason) if state in OPEN_ORDER_STATES else (None, f"{self.mode} order {state}")

    async def quote(self, m: MarketInfo, now: float, healthy: bool, health_reason: str) -> None:
        sp = self.spec
        if not sp["quote_until_s_left"] <= m.close_ts - now <= sp["quote_from_s_left"] or m.status not in TRADABLE_STATUS:
            return
        book = self.books.get(m.ticker)
        if book is None or book.age(now) > sp["max_book_age_s"]:
            return self._log(m.ticker, "skip: no book" if book is None else "skip: book older than 5 s")
        yb, ya = book.best_bid("yes"), book.best_ask("yes")
        if not yb or not ya:
            return self._log(m.ticker, "skip: one-sided book")
        lo, hi = sp["price_band"]
        # Lives until the next minute's requote, with slack for loop jitter, so an unchanged price keeps its place.
        end = min((now // sp["grid_s"] + 1) * sp["grid_s"] + 5, m.close_ts)
        for side in ("yes", "no"):
            price, queue = book.best_bid(side)
            price = round(price, 4)
            mq = (yb[0] + ya[0]) / 2 if side == "yes" else 1 - (yb[0] + ya[0]) / 2
            if self.db.one("SELECT 1 FROM quotes WHERE mode=? AND ticker=? AND side=? AND status='filled'",
                           (self.mode, m.ticker, side)):
                continue                            # one fill per side per market
            cur = self.db.one("SELECT * FROM quotes WHERE mode=? AND ticker=? AND side=? AND status='open' AND end_ts>? "
                              "ORDER BY quote_ts DESC LIMIT 1", (self.mode, m.ticker, side, now))
            if cur and abs(cur["price"] - price) < 1e-9:
                # Same price: keep resting and keep our place. Place an order if it has none working.
                self.db.x("UPDATE quotes SET end_ts=? WHERE id=?", (end, cur["id"]))
                if not self._order_live(cur["client_order_id"]):
                    coid, why = await self._place(m, side, price, healthy, health_reason)
                    self.db.x("UPDATE quotes SET client_order_id=?, risk=? WHERE id=?", (coid, why, cur["id"]))
                    if coid:
                        self._log(m.ticker, why, side, price, mq, True, 1)
                continue
            if cur:
                self.db.x("UPDATE quotes SET end_ts=? WHERE id=?", (now, cur["id"]))
            if not lo <= price <= hi:
                self._log(m.ticker, f"skip: bid {price:.2f} outside {lo:.2f}-{hi:.2f}", side, price, mq)
                continue
            coid, why = await self._place(m, side, price, healthy, health_reason)
            self.db.x("INSERT INTO quotes(mode,ticker,side,price,quote_ts,end_ts,yes_bid,yes_ask,queue,seconds_left,"
                      "client_order_id,risk) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.mode, m.ticker, side, price, now, end, yb[0], ya[0], queue, m.close_ts - now, coid, why))
            self._log(m.ticker, why, side, price, mq, bool(coid), 1)

    # --- fills from the public tape ------------------------------------------------------------
    async def poll_trades(self) -> None:
        client = self.discovery.client
        for r in self.db.q("SELECT ticker, MIN(quote_ts) t0 FROM quotes WHERE mode=? AND status='open' GROUP BY ticker",
                           (self.mode,)):
            t = r["ticker"]
            since = self._cursor.get(t, r["t0"])
            asked = time.time()
            try:
                got = [x async for x in client.trades(ticker=t, min_ts=int(since))]
            except Exception as e:
                log.warning("trades %s failed: %s", t, e)
                continue
            seen = self._seen.setdefault(t, {})
            new = []
            for x in got:
                if x["trade_id"] not in seen and not x.get("is_block_trade"):
                    seen[x["trade_id"]] = asked
                    new.append((ts(x["created_time"]), x["taker_side"], float(x["yes_price_dollars"])))
            for k in [k for k, v in seen.items() if v < asked - 300]:
                del seen[k]
            self._cursor[t], self._polled[t] = max(since, asked - self.OVERLAP_S), asked
            for trade in sorted(new):
                self.on_trade(t, *trade)

    def on_trade(self, ticker: str, t: float, taker_side: str, yes_price: float) -> None:
        """Fill every open quote this trade printed through. A taker buying our side never hits our bid,
        and a trade at exactly our price does not fill us (we do not know our place in the queue)."""
        for q in self.db.q("SELECT * FROM quotes WHERE mode=? AND ticker=? AND status='open' AND side<>? "
                           "AND quote_ts+?<=? AND end_ts>?", (self.mode, ticker, taker_side, self.spec["latency_s"], t, t)):
            ours = yes_price if q["side"] == "yes" else round(1 - yes_price, 4)
            if ours >= q["price"] - 1e-9:
                continue
            # shortcut: a trade that reaches the API late can fill an older quote after a newer one on the same
            # side filled. The older one is then skipped, so one side never fills twice. Rare (needs > 5 s lag).
            if self.db.one("SELECT 1 FROM quotes WHERE mode=? AND ticker=? AND side=? AND status='filled'",
                           (self.mode, ticker, q["side"])):
                continue
            with self.db.tx():
                self.db.x("UPDATE quotes SET status='filled', fill_ts=?, fill_trade_price=? WHERE id=?",
                          (t, ours, q["id"]))
                self.db.x("UPDATE quotes SET end_ts=MIN(end_ts, ?) WHERE mode=? AND ticker=? AND side=? AND status='open'",
                          (t, self.mode, ticker, q["side"]))
            if q["client_order_id"] and self.paper:
                self.adapter.fill(q["client_order_id"], q["price"], self._fee(ticker, q["price"]))

    async def expire(self) -> None:
        """Close quotes that ended, once the tape has been read past their end."""
        for q in self.db.q("SELECT q.id, q.ticker, q.end_ts, q.client_order_id, o.exchange_order_id FROM quotes q "
                           "LEFT JOIN orders o ON o.client_order_id=q.client_order_id "
                           "WHERE q.mode=? AND q.status='open'", (self.mode,)):
            if self._polled.get(q["ticker"], 0) - self.API_LAG_S >= q["end_ts"]:
                self.db.x("UPDATE quotes SET status='expired' WHERE id=?", (q["id"],))
                if q["exchange_order_id"] and self.paper:
                    await self.adapter.cancel(q["ticker"], q["exchange_order_id"])

    async def cancel_ended(self, now: float) -> None:
        """Real orders: cancel at once when their quote has ended, filled on the tape or not."""
        for q in self.db.q(f"SELECT q.client_order_id FROM quotes q JOIN orders o ON o.client_order_id=q.client_order_id "
                           f"WHERE q.mode=? AND q.end_ts<=? AND o.state IN ({','.join('?' * len(OPEN_ORDER_STATES))})",
                           (self.mode, now, *OPEN_ORDER_STATES)):
            try:
                await self.om.cancel(q["client_order_id"])
            except Exception as e:      # reconciliation and the next tick retry
                log.error("cancel %s failed: %s", q["client_order_id"], e)

    async def fetch_results(self, now: float) -> None:
        """Outcomes for markets that only the research log or the fair_value shadow touched."""
        rows = self.db.q(
            "SELECT DISTINCT m.ticker FROM markets m WHERE m.close_ts < ? AND COALESCE(m.result,'') NOT IN ('yes','no') "
            "AND (m.ticker IN (SELECT ticker FROM quotes WHERE mode=? AND status='filled') "
            "OR m.ticker IN (SELECT ticker FROM signals WHERE mode=? AND strategy='fair_value' AND reason=?)) LIMIT 10",
            (now - 60, self.mode, self.mode, SHADOW_REASON))
        for r in rows:
            try:
                await self.discovery.check_result(r["ticker"])
            except Exception as e:
                log.warning("result %s failed: %s", r["ticker"], e)


# --- the frozen 14-day paper test (P1) --------------------------------------------------------

# Frozen 2026-10-23 before any P1 data exists. Changing anything here makes a new test with a new hash.
PAPER_TEST = {
    "name": "P1",
    "question": "Does E2's maker rule, placed under the configured $25 paper limits, earn after fees on fresh "
                "live data?",
    "series": list(SERIES),
    "quote_rule": "E2's, unchanged: at each minute from 840 s to 60 s before close, a 1-contract bid at the best "
                  "YES bid and one at the best NO bid, prices 0.05 to 0.95, each resting until the next minute",
    "fill_rule": "a bid fills only when a public trade prints through its price at least 1 s after posting. A "
                 "trade at our price never fills it. At most one fill per side per market",
    "exit_rule": "none: every fill is held to settlement",
    "fees": "the series' own fee (quadratic: zero for makers). Stress case: 0.0175 x p x (1 - p) per contract",
    "slippage": "none: a maker fill is at the bid price or not at all",
    "risk_limits": {"bankroll": 25.0, "max_position_risk_pct": 0.01, "max_open_risk_pct": 0.05,
                    "max_daily_loss_pct": 0.02, "max_consecutive_losses": 5, "max_open_positions": 3,
                    "max_market_inventory": 1},
    "start": "the bot's first minute running on a fresh database created after this spec. Nothing earlier counts",
    "days": 14,
    "extend_by_gaps_over_s": 600,       # downtime never shortens the test
    "void_if_gap_over_s": 7200,         # then start again with a fresh database
    "stop_loss_usd": 5.0,               # account net loss that ends the test early as a FAIL
    "primary": "paper account, what the limits let it place: net per settled contract, 95% CI over whole windows",
    "secondary": "research log, every quote at 1 contract with no limits; fair_value shadow; no trade",
    "pass": "account: 50+ settled trades, 95% CI above zero, mean above zero at the stress fee. Research log: "
            "300+ fills over 100+ windows, 95% CI above zero, mean above zero at the stress fee. No gap over 2 h, "
            "risk limits unchanged for the whole test",
}


def paper_test_hash() -> str:
    return hashlib.sha256(json.dumps(PAPER_TEST, sort_keys=True).encode()).hexdigest()[:12]


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def paper_test(db: DB, mode: str, log_rows: list[dict], net_pnl: float, now: float | None = None) -> dict:
    """P1's state: where it stands, its data gaps, restarts, halts and settings, and the pass checks."""
    now = now or time.time()
    sp, out = PAPER_TEST, {"spec_hash": paper_test_hash()}
    # The running bot writes equity every 60 s, so a stretch without rows is time it was down or stuck.
    beats = [r["ts"] for r in db.q("SELECT ts FROM equity WHERE mode=? ORDER BY ts", (mode,))]
    if not beats:
        return {**out, "state": "not started"}
    t0 = beats[0]
    gaps = [(a, b - a - 60) for a, b in zip(beats, beats[1:] + [now]) if b - a - 60 > sp["extend_by_gaps_over_s"]]
    end = t0 + sp["days"] * 86400 + sum(g for _, g in gaps)
    restarts = db.q("SELECT data FROM events WHERE kind='interruption' AND mode=? AND ts>=?", (mode, t0))
    down = [json.loads(r["data"])["down_s"] for r in restarts]
    halted, since = 0.0, None
    for e in db.q("SELECT ts, kind FROM events WHERE mode=? AND kind IN ('risk_trip','risk_clear') AND ts>=? "
                  "ORDER BY ts", (mode, t0)):
        if e["kind"] == "risk_trip" and since is None:
            since = e["ts"]
        elif e["kind"] == "risk_clear" and since is not None:
            halted, since = halted + e["ts"] - since, None
    halted += now - since if since is not None else 0
    limits = {json.dumps(json.loads(r["data"]).get("limits"), sort_keys=True)
              for r in db.q("SELECT data FROM events WHERE kind='startup' AND mode=? AND ts>=?", (mode, t0 - 300))}
    unchanged = limits == {json.dumps(sp["risk_limits"], sort_keys=True)}
    acct = []
    for p in db.q("SELECT pnl, count, cost, close_ts FROM positions WHERE mode=? AND status='settled' AND count>0",
                  (mode,)):
        c = p["cost"] / p["count"]
        acct.append({"window": p["close_ts"], "net": p["pnl"] / p["count"],
                     "net_stress": p["pnl"] / p["count"] - MAKER_RATE * c * (1 - c)})
    a, lg = summary(acct, "net"), summary(log_rows, "net")
    void = any(g > sp["void_if_gap_over_s"] for _, g in gaps)
    checks = {
        "account_50_trades": a["n"] >= 50,
        "account_ci_above_zero": bool(a["ci"] and a["ci"][0] > 0),
        "account_positive_at_stress_fee": (summary(acct, "net_stress")["mean"] or 0) > 0,
        "log_300_fills_100_windows": lg["n"] >= 300 and lg["windows"] >= 100,
        "log_ci_above_zero": bool(lg["ci"] and lg["ci"][0] > 0),
        "log_positive_at_stress_fee": (summary(log_rows, "net_if_maker_fee")["mean"] or 0) > 0,
        "no_gap_over_2h": not void,
        "risk_limits_unchanged": unchanged,
    }
    if void:
        state = "VOID: a gap over 2 hours. Start again with a fresh database"
    elif net_pnl <= -sp["stop_loss_usd"]:
        state = f"FAIL: the account lost ${-net_pnl:.2f}, past the ${sp['stop_loss_usd']:g} stop"
    elif now >= end:
        state = "PASS" if all(checks.values()) else "FAIL"
    else:
        state = f"collecting: day {(now - t0) / 86400:.1f} of {sp['days']}"
    return {**out, "state": state, "start_utc": _iso(t0), "end_utc": _iso(end),
            "gaps_over_10_min": len(gaps), "longest_gap_min": round(max((g for _, g in gaps), default=0) / 60, 1),
            "restarts": len(down), "longest_restart_down_s": max(down, default=0), "halted_h": round(halted / 3600, 2),
            "risk_limits_unchanged": unchanged,
            "executable": {"what": "paper account at the configured limits", "net_per_contract": a,
                           "net_at_stress_fee": summary(acct, "net_stress")["mean"]},
            "research_only": {"what": "every quote at 1 contract, no limits", "net_per_contract": lg,
                              "net_at_stress_fee": summary(log_rows, "net_if_maker_fee")["mean"]},
            "checks": checks}


# --- report -----------------------------------------------------------------------------------

def _won(side, result):
    return 1.0 if side == result else 0.0


def report(db: DB, mode: str = "paper", bankroll: float = 25.0) -> dict:
    """The maker rule's research log, the risk-limited paper account, the fair_value shadow and the
    no-trade baseline, side by side. Gross and net are separate. CIs resample whole windows."""
    res = {r["ticker"]: r for r in db.q("SELECT ticker, series, result, close_ts, fee_type, fee_multiplier "
                                        "FROM markets WHERE result IN ('yes','no')")}
    quotes = db.q("SELECT * FROM quotes WHERE mode=?", (mode,))
    rows = []
    for f in quotes:
        m = res.get(f["ticker"])
        if f["status"] != "filled" or not m:
            continue
        p, mid = f["price"], (f["yes_bid"] + f["yes_ask"]) / 2
        gross = _won(f["side"], m["result"]) - p
        rows.append({"window": m["close_ts"], "series": m["series"], "gross": gross,
                     "spread_earned": (mid if f["side"] == "yes" else 1 - mid) - p,
                     "net": gross - fee(p, 1, maker=True, fee_type=m["fee_type"] or "quadratic",
                                        multiplier=m["fee_multiplier"] or 1.0),
                     "net_if_maker_fee": gross - MAKER_RATE * p * (1 - p),
                     "net_if_taker_fee": gross - TAKER_RATE * p * (1 - p)})
    for r in rows:
        r["adverse_selection"] = r["gross"] - r["spread_earned"]
    fv = []
    first = db.q("SELECT ticker, outcome_side, entry_price FROM signals WHERE id IN (SELECT MIN(id) FROM signals "
                 "WHERE mode=? AND strategy='fair_value' AND reason=? GROUP BY ticker)", (mode, SHADOW_REASON))
    for s in first:
        m = res.get(s["ticker"])
        if m:
            gross = _won(s["outcome_side"], m["result"]) - s["entry_price"]
            fv.append({"window": m["close_ts"], "gross": gross, "net": gross - fee(s["entry_price"], 1)})
    perf = performance(db, mode, bankroll)
    paper_gross = db.one("SELECT COALESCE(SUM(pnl+fees),0) g FROM positions WHERE mode=? AND status='settled'",
                         (mode,))["g"]
    skips = {}
    for r in db.q("SELECT reason FROM signals WHERE mode=? AND strategy='maker' AND accepted=0 AND ts>?",
                  (mode, time.time() - 86400)):
        k = re.sub(r"\$?\d+(\.\d+)?", "#", r["reason"] or "")
        skips[k] = skips.get(k, 0) + 1
    t0 = db.one("SELECT MIN(quote_ts) t FROM quotes WHERE mode=?", (mode,))["t"]
    # Real orders only: did the exchange fill the bids the tape rule says were filled? Paper fills are the tape's.
    check = {"tape_and_exchange": 0, "tape_only": 0, "exchange_only": 0, "neither": 0}
    for r in db.q("SELECT q.status, o.filled FROM quotes q JOIN orders o ON o.client_order_id=q.client_order_id "
                  "WHERE q.mode=? AND q.status<>'open' AND o.state IN ('filled','canceled')", (mode,)):
        tape, ex = r["status"] == "filled", (r["filled"] or 0) > 0
        check["tape_and_exchange" if tape and ex else "tape_only" if tape else "exchange_only" if ex else "neither"] += 1
    return {
        "label": PAPER_LABEL if mode == "paper" else LIVE_LABEL.format(mode=mode.upper()),
        "since_utc": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d %H:%M") if t0 else None,
        "maker_research_log": {
            "what": "every quote the rule posted, 1 contract each, no risk limits",
            "quotes": len(quotes), "fills": sum(q["status"] == "filled" for q in quotes), "settled_fills": len(rows),
            "gross_per_contract": summary(rows, "gross"), "net_per_contract": summary(rows, "net"),
            "net_if_maker_fee_0.0175": summary(rows, "net_if_maker_fee")["mean"],
            "net_if_taker_fee": summary(rows, "net_if_taker_fee")["mean"],
            "spread_earned": summary(rows, "spread_earned")["mean"],
            "adverse_selection": summary(rows, "adverse_selection")["mean"],
        },
        "paper_account": {
            "what": f"risk-limited, bankroll ${bankroll:g}",
            "settled_trades": perf["settled_trades"], "gross_pnl": round(paper_gross, 4), "fees": perf["fees"],
            "net_pnl": perf["net_pnl"], "max_drawdown": perf["max_drawdown"],
            "open_positions": db.one("SELECT COUNT(*) n FROM positions WHERE mode=? AND status='open'", (mode,))["n"],
            "resting_orders": db.one("SELECT COUNT(*) n FROM orders WHERE mode=? AND state='resting'", (mode,))["n"],
        },
        "fair_value_shadow": {
            "what": "first signal per market, 1 contract at the ask, taker fee",
            "signals": len(first), "settled": len(fv), "gross_per_contract": summary(fv, "gross"),
            "net_per_contract": summary(fv, "net")},
        "no_trade_baseline": {"net_pnl": 0.0},
        "paper_test": paper_test(db, mode, rows, perf["net_pnl"]),
        "skips_24h": dict(sorted(skips.items(), key=lambda kv: -kv[1])[:8]),
        **({} if mode == "paper" else {"fill_model_check": check}),
    }


def format_report(rep: dict) -> str:
    """A few lines for `kbot status`."""
    def ci(sm):
        if not sm or sm.get("mean") is None:
            return "n/a"
        lo, hi = sm["ci"] or (None, None)
        return f"{sm['mean']:+.4f}" + (f" (95% CI {lo:+.4f} to {hi:+.4f})" if lo is not None else "")

    def num(x):
        return "n/a" if x is None else f"{x:+.4f}"
    lg, pa, fv = rep["maker_research_log"], rep["paper_account"], rep["fair_value_shadow"]
    lines = [rep["label"], f"Since: {rep['since_utc']} UTC", "",
             f"Maker rule, every quote at 1 contract: {lg['quotes']} quotes, {lg['fills']} fills, "
             f"{lg['settled_fills']} settled in {lg['gross_per_contract']['windows']} windows",
             f"  gross per contract {ci(lg['gross_per_contract'])}",
             f"  net per contract   {ci(lg['net_per_contract'])} (this series' maker fee)",
             f"  net if a 0.0175 maker fee applied {num(lg['net_if_maker_fee_0.0175'])}, "
             f"at the taker fee {num(lg['net_if_taker_fee'])}",
             f"  spread earned {num(lg['spread_earned'])}, adverse selection {num(lg['adverse_selection'])}",
             f"Paper account ({pa['what']}): {pa['settled_trades']} settled, gross {pa['gross_pnl']:+.2f}, "
             f"fees {pa['fees']:.2f}, net {pa['net_pnl']:+.2f}, max drawdown {pa['max_drawdown']:.2f}, "
             f"{pa['open_positions']} open, {pa['resting_orders']} resting",
             f"fair_value shadow ({fv['what']}): {fv['signals']} signals, {fv['settled']} settled",
             f"  gross per contract {ci(fv['gross_per_contract'])}",
             f"  net per contract   {ci(fv['net_per_contract'])}",
             "No-trade baseline: net 0.00", ""]
    if (pt := rep.get("paper_test")) and pt.get("start_utc"):
        ex, rs = pt["executable"], pt["research_only"]
        lines += [f"P1 frozen paper test (spec {pt['spec_hash']}): {pt['state']}. "
                  f"{pt['start_utc']} to {pt['end_utc']}",
                  f"  Executable ({ex['what']}): net per contract {ci(ex['net_per_contract'])}, "
                  f"{ex['net_per_contract']['n']} trades, at the stress fee {num(ex['net_at_stress_fee'])}",
                  f"  Research only ({rs['what']}): net per contract {ci(rs['net_per_contract'])}",
                  f"  Data: {pt['gaps_over_10_min']} gaps over 10 min (longest {pt['longest_gap_min']} min), "
                  f"{pt['restarts']} restarts (longest down {pt['longest_restart_down_s']:.0f} s), "
                  f"halted {pt['halted_h']} h, risk limits {'unchanged' if pt['risk_limits_unchanged'] else 'CHANGED'}",
                  "  Checks: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in pt["checks"].items()), ""]
    lines += ["Skipped quotes, last 24 h:"]
    lines += [f"  {n:6d}  {k}" for k, n in rep["skips_24h"].items()] or ["  none"]
    if (c := rep.get("fill_model_check")):
        lines += ["", "Fill model check, closed quotes with a real order (tape rule vs exchange): "
                  + ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in c.items())]
    return "\n".join(lines)

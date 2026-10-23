"""Central risk manager. Every order passes `approve` here; strategies cannot skip it.

Risk is measured as worst-case loss: for a bought binary contract that is the price
paid plus fees (it can settle at $0). Resting and partially filled orders count at their
full remaining size. State lives in SQLite so a restart cannot reset the daily loss.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import RiskLimits
from .db import DB, utc_day
from .fees import fee
from .strategy import Candidate, contracts_within

log = logging.getLogger(__name__)

OPEN_ORDER_STATES = ("pending_submit", "submitted", "resting", "partially_filled", "unknown")


@dataclass
class Decision:
    approved: bool
    count: float
    reason: str
    trip: str | None = None      # set when a hard limit was hit and trading must halt


class RiskManager:
    def __init__(self, db: DB, limits: RiskLimits, mode: str, clock=time.time):
        self.db, self.limits, self.mode, self.clock = db, limits, mode, clock

    # --- persistent switches ------------------------------------------------
    def halted(self) -> str | None:
        for key in ("estop", f"halt:{self.mode}", "pause"):
            v = self.db.get(key)
            if v:
                return f"{key}: {v}"
        return None

    def trip(self, reason: str) -> None:
        if not self.db.get(f"halt:{self.mode}"):
            self.db.set(f"halt:{self.mode}", {"reason": reason, "ts": self.clock()})
            self.db.event("risk_trip", self.mode, reason=reason)
            log.warning("RISK TRIP (%s): %s", self.mode, reason)

    def clear(self, who: str) -> None:
        self.db.set(f"halt:{self.mode}", None)
        self.db.set(f"review_ack:{self.mode}", self.clock())   # loss streak restarts after review
        self.db.event("risk_clear", self.mode, by=who)

    # --- exposure -----------------------------------------------------------
    def open_risk(self) -> float:
        pos = self.db.one("SELECT COALESCE(SUM(cost+fees),0) r FROM positions WHERE mode=? AND status='open'",
                          (self.mode,))["r"]
        orders = self.db.q(
            f"SELECT limit_price, count, filled FROM orders WHERE mode=? AND purpose='entry' "
            f"AND state IN ({','.join('?' * len(OPEN_ORDER_STATES))})", (self.mode, *OPEN_ORDER_STATES))
        resting = sum((o["count"] - o["filled"]) * o["limit_price"]
                      + fee(o["limit_price"], o["count"] - o["filled"]) for o in orders
                      if o["count"] > o["filled"])
        return pos + resting

    def busy_tickers(self) -> set[str]:
        rows = self.db.q(
            f"SELECT ticker FROM positions WHERE mode=? AND status='open' UNION "
            f"SELECT ticker FROM orders WHERE mode=? AND state IN ({','.join('?' * len(OPEN_ORDER_STATES))})",
            (self.mode, self.mode, *OPEN_ORDER_STATES))
        return {r["ticker"] for r in rows}

    def consecutive_losses(self) -> int:
        n = 0
        since = self.db.get(f"review_ack:{self.mode}", 0) or 0
        for r in self.db.q("SELECT pnl FROM positions WHERE mode=? AND status='settled' AND settled_at>? "
                           "ORDER BY settled_at DESC LIMIT 50", (self.mode, since)):
            if r["pnl"] is None or r["pnl"] >= 0:
                break
            n += 1
        return n

    def daily_loss(self) -> float:
        return -min(0.0, self.db.daily_realized(self.mode, utc_day(self.clock())))

    # --- the gate -----------------------------------------------------------
    def check_limits(self) -> str | None:
        """Hard limits that halt trading. Returns the trip reason, if any."""
        L = self.limits
        if self.daily_loss() >= L.max_daily_loss_pct * L.bankroll - 1e-9:
            return f"daily realized loss {self.daily_loss():.2f} >= {L.max_daily_loss_pct * L.bankroll:.2f}"
        if self.consecutive_losses() >= L.max_consecutive_losses:
            return f"{self.consecutive_losses()} consecutive losses: review required"
        return None

    def approve_quote(self, ticker: str, side: str, price: float, count: float, fee_paid: float, *,
                      healthy: bool = True, health_reason: str = "") -> Decision:
        """Gate for a resting maker bid. Worst case: it fills and settles at $0."""
        L = self.limits
        if (h := self.halted()):
            return Decision(False, 0, f"halted ({h})")
        if not healthy:
            return Decision(False, 0, f"unhealthy: {health_reason}")
        if (t := self.check_limits()):
            self.trip(t)
            return Decision(False, 0, t, trip=t)
        if not 0 < price < 1 or count < 1:
            return Decision(False, 0, "invalid price or size")
        held = self.db.one("SELECT COALESCE(SUM(count),0) n FROM positions WHERE mode=? AND ticker=? "
                           "AND outcome_side=? AND status='open'", (self.mode, ticker, side))["n"]
        if held + count > L.max_market_inventory:
            return Decision(False, 0, f"inventory {held:.0f} + {count:.0f} > {L.max_market_inventory} per market side")
        busy = self.busy_tickers() - {ticker}
        if len(busy) >= L.max_open_positions:
            return Decision(False, 0, f"{len(busy)} other markets with positions or orders >= {L.max_open_positions}")
        worst = count * price + fee_paid
        budget = min(L.max_position_risk_pct * L.bankroll, L.max_open_risk_pct * L.bankroll - self.open_risk())
        if worst > budget + 1e-9:
            return Decision(False, 0, f"worst-case loss ${worst:.2f} > risk budget ${budget:.2f}")
        return Decision(True, float(count), f"approved {count:.0f} at {price:.2f}, worst-case loss "
                                            f"${worst:.2f} of ${budget:.2f} budget")

    def approve(self, c: Candidate, *, healthy: bool = True, health_reason: str = "") -> Decision:
        L = self.limits
        if (h := self.halted()):
            return Decision(False, 0, f"halted ({h})")
        if not healthy:
            return Decision(False, 0, f"unhealthy: {health_reason}")
        if (t := self.check_limits()):
            self.trip(t)
            return Decision(False, 0, t, trip=t)
        if not 0 < c.entry_price < 1 or c.max_entry_price < c.entry_price:
            return Decision(False, 0, "invalid price")
        busy = self.busy_tickers()
        if c.ticker in busy:
            return Decision(False, 0, "already have a position or order in this market (no averaging)")
        if len(busy) >= L.max_open_positions:
            return Decision(False, 0, f"{len(busy)} open positions >= {L.max_open_positions}")
        # Size against worst-case loss at the max price we allow, not the hoped-for price.
        per_pos = L.max_position_risk_pct * L.bankroll
        headroom = L.max_open_risk_pct * L.bankroll - self.open_risk()
        budget = min(per_pos, headroom)
        p = c.max_entry_price
        fee_pc = fee(p, max(c.count, 1)) / max(c.count, 1)
        n = min(c.count, contracts_within(budget, p, fee_pc))
        # Re-check with the actual fee for n contracts (per-order rounding).
        while n >= 1 and n * p + fee(p, n) > budget + 1e-9:
            n -= 1
        if n < 1:
            return Decision(False, 0, f"risk budget ${budget:.2f} too small for one contract at {p:.2f}")
        return Decision(True, float(n), f"approved {n} at <= {p:.2f}, worst-case loss "
                                        f"${n * p + fee(p, n):.2f} of ${budget:.2f} budget")

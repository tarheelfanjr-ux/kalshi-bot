"""Order execution: an explicit order state machine plus exchange adapters.

Rules this module enforces:
* The order row is written (pending_submit) before anything is sent, so a crash can
  never produce an order the database does not know about.
* A create request that times out becomes `unknown`. It is never resent. Reconciliation
  looks it up by client_order_id first.
* An accepted order is not a filled order. Fills come from the exchange response or
  the reconciliation query, never from assumption.
* Only immediate-or-cancel taker orders are used for entries, so nothing rests on the
  book by accident. Resting orders are still tracked and cancellable.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from .book import OrderBook
from .client import KalshiError, OrderStateUnknown, TradingClient
from .db import DB
from .fees import fee

log = logging.getLogger(__name__)

TRANSITIONS = {
    "pending_submit": {"submitted", "filled", "partially_filled", "resting", "canceled", "rejected", "unknown"},
    "unknown": {"submitted", "filled", "partially_filled", "resting", "canceled", "rejected"},
    "submitted": {"resting", "partially_filled", "filled", "canceled", "rejected", "unknown"},
    "resting": {"partially_filled", "filled", "canceled", "unknown"},
    "partially_filled": {"partially_filled", "filled", "canceled", "unknown"},
    "filled": set(), "canceled": set(), "rejected": set(),
}
TERMINAL = {"filled", "canceled", "rejected"}


class IllegalTransition(Exception):
    pass


@dataclass
class ExchangeOrder:
    """What an adapter reports about an order."""
    exchange_order_id: str | None
    status: str            # resting | canceled | executed | rejected
    filled: float
    remaining: float
    avg_price: float | None   # in the order's outcome-side price
    fees: float


class Adapter(Protocol):
    mode: str

    async def submit(self, *, ticker, outcome_side, limit_price, count, client_order_id) -> ExchangeOrder: ...
    async def cancel(self, ticker, exchange_order_id) -> None: ...
    async def lookup(self, ticker, client_order_id, exchange_order_id) -> ExchangeOrder | None: ...
    async def positions(self) -> dict[str, float] | None: ...


class PaperAdapter:
    """Simulated exchange on live books. Never touches the network.

    Fill model (conservative): an IOC buy takes only `fill_fraction` of the displayed size at
    each level at or under the limit, to account for latency and other takers. Taker fees.
    """
    mode = "paper"

    def __init__(self, get_book: Callable[[str], OrderBook | None], fill_fraction: float = 0.5,
                 max_book_age_s: float = 5.0):
        self.get_book, self.fill_fraction, self.max_age = get_book, fill_fraction, max_book_age_s
        self.orders: dict[str, ExchangeOrder] = {}

    async def submit(self, *, ticker, outcome_side, limit_price, count, client_order_id):
        book = self.get_book(ticker)
        if book is None or book.age() > self.max_age:
            eo = ExchangeOrder(None, "rejected", 0, count, None, 0)
        else:
            filled = cost = 0.0
            for price, size in book.asks(outcome_side):
                if price > limit_price + 1e-9 or filled >= count:
                    break
                take = min(int(size * self.fill_fraction), count - filled)
                filled += take
                cost += take * price
            avg = round(cost / filled, 4) if filled else None
            fees = fee(avg, filled) if filled else 0.0
            eo = ExchangeOrder("paper-" + client_order_id[:8], "executed" if filled else "canceled",
                               filled, 0, avg, fees)
        self.orders[client_order_id] = eo
        return eo

    async def cancel(self, ticker, exchange_order_id):
        return None

    async def lookup(self, ticker, client_order_id, exchange_order_id):
        return self.orders.get(client_order_id)

    async def positions(self):
        return None   # the local DB is the only book of record in paper mode


class KalshiAdapter:
    """Real orders on the demo or production exchange through a TradingClient."""

    def __init__(self, client: TradingClient):
        self.client = client
        self.mode = client.environment     # "demo" or "production"

    @staticmethod
    def _to_book(outcome_side: str, limit_price: float) -> tuple[str, float]:
        # V2 orders are expressed on the YES leg: buying NO at p is an ask at 1 - p.
        return ("bid", limit_price) if outcome_side == "yes" else ("ask", round(1 - limit_price, 4))

    @staticmethod
    def _from_api(o: dict, outcome_side: str) -> ExchangeOrder:
        filled = float(o.get("fill_count_fp") or o.get("fill_count") or 0)
        remaining = float(o.get("remaining_count_fp") or o.get("remaining_count") or 0)
        fees = float(o.get("taker_fees_dollars") or 0) + float(o.get("maker_fees_dollars") or 0)
        avg = None
        if filled:
            cost = float(o.get("taker_fill_cost_dollars") or 0) + float(o.get("maker_fill_cost_dollars") or 0)
            if cost:
                avg = cost / filled
            elif o.get("average_fill_price"):
                yes_avg = float(o["average_fill_price"])
                avg = yes_avg if outcome_side == "yes" else 1 - yes_avg
            if not fees and o.get("average_fee_paid"):
                fees = float(o["average_fee_paid"]) * filled
        status = o.get("status") or ("executed" if remaining == 0 and filled else "resting" if remaining else "canceled")
        return ExchangeOrder(o.get("order_id"), status, filled, remaining, avg, fees)

    async def submit(self, *, ticker, outcome_side, limit_price, count, client_order_id):
        book_side, price = self._to_book(outcome_side, limit_price)
        r = await self.client.create_order(ticker=ticker, book_side=book_side, price=price, count=count,
                                           client_order_id=client_order_id)
        eo = self._from_api(r, outcome_side)
        if eo.status == "resting" and eo.remaining == 0:
            eo.status = "executed"
        # IOC: anything not filled was cancelled by the exchange.
        if eo.remaining == 0 and eo.filled == 0:
            eo.status = "canceled"
        return eo

    async def cancel(self, ticker, exchange_order_id):
        try:
            await self.client.cancel_order(exchange_order_id, ticker)
        except KalshiError as e:
            if e.status != 404:
                raise

    async def lookup(self, ticker, client_order_id, exchange_order_id):
        o = None
        if exchange_order_id:
            try:
                o = await self.client.order(exchange_order_id)
            except KalshiError as e:
                if e.status != 404:
                    raise
        if o is None:
            o = await self.client.find_order_by_client_id(ticker, client_order_id)
        if o is None:
            return None
        return self._from_api(o, o.get("outcome_side") or "yes")

    async def positions(self):
        out = {}
        async for p in self.client.positions(count_filter="position"):
            n = float(p.get("position_fp") or 0)
            if n:
                out[p["ticker"]] = n     # + YES, - NO
        return out


class OrderManager:
    def __init__(self, db: DB, adapter: Adapter, notify: Callable[[str], None] | None = None):
        self.db, self.adapter = db, adapter
        self.mode = adapter.mode
        self.notify = notify or (lambda msg: None)

    # --- state machine ------------------------------------------------------
    def _set_state(self, coid: str, new: str, **fields) -> None:
        row = self.db.one("SELECT state FROM orders WHERE client_order_id=?", (coid,))
        old = row["state"]
        if new != old and new not in TRANSITIONS[old]:
            raise IllegalTransition(f"{coid}: {old} -> {new}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.x(f"UPDATE orders SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE client_order_id=?",
                  (new, time.time(), *fields.values(), coid))
        if new != old:
            self.db.event("order_state", self.mode, coid=coid, old=old, new=new, **fields)

    def _apply(self, coid: str, eo: ExchangeOrder) -> None:
        """Bring the local order and position in line with what the exchange reported."""
        with self.db.tx():
            o = self.db.one("SELECT * FROM orders WHERE client_order_id=?", (coid,))
            new_fill = max(0.0, eo.filled - o["filled"])
            if new_fill > 0:
                avg = eo.avg_price if eo.avg_price is not None else o["limit_price"]
                prev_cost = (o["avg_price"] or 0) * o["filled"]
                fill_cost = avg * eo.filled - prev_cost
                fill_fee = max(0.0, eo.fees - o["fees"])
                self.db.x("INSERT OR IGNORE INTO fills(fill_id,client_order_id,mode,ticker,outcome_side,count,"
                          "price,fee,is_taker,ts) VALUES(?,?,?,?,?,?,?,?,1,?)",
                          (f"{coid}:{eo.filled}", coid, self.mode, o["ticker"], o["outcome_side"], new_fill,
                           fill_cost / new_fill, fill_fee, time.time()))
                close_ts = self.db.one("SELECT close_ts FROM markets WHERE ticker=?", (o["ticker"],))
                self.db.x(
                    "INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,close_ts,status,opened_at) "
                    "VALUES(?,?,?,?,?,?,?,'open',?) ON CONFLICT(mode,ticker,outcome_side) DO UPDATE SET "
                    "count=count+excluded.count, cost=cost+excluded.cost, fees=fees+excluded.fees",
                    (self.mode, o["ticker"], o["outcome_side"], new_fill, fill_cost, fill_fee,
                     close_ts["close_ts"] if close_ts else None, time.time()))
            if eo.status == "rejected":
                state = "rejected"
            elif eo.remaining > 0 and eo.status == "resting":
                state = "partially_filled" if eo.filled > 0 else "resting"
            elif eo.filled >= o["count"] - 1e-9:
                state = "filled"
            else:  # IOC remainder cancelled, with or without a partial fill
                state = "canceled"
            self._set_state(coid, state, exchange_order_id=eo.exchange_order_id or o["exchange_order_id"],
                            filled=eo.filled, avg_price=eo.avg_price, fees=eo.fees)

    # --- public API ---------------------------------------------------------
    async def place(self, *, ticker: str, outcome_side: str, limit_price: float, count: float,
                    signal_id: int | None = None, purpose: str = "entry") -> str:
        coid = str(uuid.uuid4())
        self.db.x("INSERT INTO orders(client_order_id,mode,ticker,outcome_side,limit_price,count,state,purpose,"
                  "signal_id,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending_submit',?,?,?,?)",
                  (coid, self.mode, ticker, outcome_side, limit_price, count, purpose, signal_id,
                   time.time(), time.time()))
        self.db.event("order_submit", self.mode, ticker, coid=coid, side=outcome_side, limit=limit_price, count=count)
        try:
            eo = await self.adapter.submit(ticker=ticker, outcome_side=outcome_side, limit_price=limit_price,
                                           count=count, client_order_id=coid)
        except OrderStateUnknown as e:
            self._set_state(coid, "unknown", last_error=str(e)[:500])
            self.notify(f"order {coid} state unknown after timeout; will reconcile, not resend")
            return coid
        except KalshiError as e:
            self._set_state(coid, "rejected", last_error=str(e)[:500])
            if "insufficient" in str(e.body).lower():
                self.db.set(f"halt:{self.mode}", {"reason": "insufficient funds", "ts": time.time()})
                self.notify("insufficient funds: entries halted")
            return coid
        self._apply(coid, eo)
        return coid

    async def reconcile_orders(self) -> list[str]:
        """Resolve every non-terminal order against the exchange. Returns problems found."""
        problems = []
        for o in self.db.q("SELECT * FROM orders WHERE mode=? AND state NOT IN ('filled','canceled','rejected')",
                           (self.mode,)):
            eo = await self.adapter.lookup(o["ticker"], o["client_order_id"], o["exchange_order_id"])
            if eo is None:
                age = time.time() - (o["created_at"] or 0)
                if o["state"] in ("unknown", "pending_submit") and age > 60:
                    # Not on the exchange a minute later: it never arrived.
                    self._set_state(o["client_order_id"], "rejected", last_error="not found on exchange")
                elif o["state"] not in ("unknown", "pending_submit"):
                    problems.append(f"order {o['client_order_id']} missing on exchange")
                continue
            self._apply(o["client_order_id"], eo)
        self.db.set(f"last_reconcile:{self.mode}", time.time())
        return problems

    async def reconcile_positions(self) -> list[str]:
        remote = await self.adapter.positions()
        if remote is None:
            return []
        local: dict[str, float] = {}
        closed: set[str] = set()   # closed markets are mid-settlement; settlement handles them
        for p in self.db.q("SELECT ticker, outcome_side, count, close_ts FROM positions WHERE mode=? AND status='open'",
                           (self.mode,)):
            local[p["ticker"]] = local.get(p["ticker"], 0) + (p["count"] if p["outcome_side"] == "yes" else -p["count"])
            if p["close_ts"] and p["close_ts"] < time.time():
                closed.add(p["ticker"])
        problems = []
        for t in (set(local) | set(remote)) - closed:
            if abs(local.get(t, 0) - remote.get(t, 0)) > 1e-6:
                problems.append(f"{t}: local {local.get(t, 0)} vs exchange {remote.get(t, 0)}")
        return problems

    async def cancel_open_entries(self, reason: str) -> int:
        n = 0
        for o in self.db.q("SELECT * FROM orders WHERE mode=? AND purpose='entry' "
                           "AND state IN ('resting','partially_filled','submitted')", (self.mode,)):
            try:
                await self.adapter.cancel(o["ticker"], o["exchange_order_id"])
                n += 1
            except Exception as e:  # keep going; reconciliation will catch leftovers
                log.error("cancel %s failed: %s", o["client_order_id"], e)
        self.db.event("cancel_open_entries", self.mode, reason=reason, count=n)
        if n:
            await self.reconcile_orders()
        return n

    def settle(self, ticker: str, result: str, ts: float | None = None) -> float:
        """Book settlement for local positions in a market. Returns realized P&L."""
        if result not in ("yes", "no"):
            return 0.0
        total = 0.0
        with self.db.tx():
            for p in self.db.q("SELECT * FROM positions WHERE mode=? AND ticker=? AND status='open'",
                               (self.mode, ticker)):
                payout = p["count"] * (1.0 if p["outcome_side"] == result else 0.0)
                pnl = payout - p["cost"] - p["fees"]
                self.db.x("UPDATE positions SET status='settled', result=?, pnl=?, settled_at=? "
                          "WHERE mode=? AND ticker=? AND outcome_side=?",
                          (result, pnl, ts or time.time(), self.mode, ticker, p["outcome_side"]))
                self.db.ledger(self.mode, "settlement", pnl, ticker, ts)
                self.db.event("settlement", self.mode, ticker, side=p["outcome_side"], result=result,
                              count=p["count"], pnl=round(pnl, 4))
                total += pnl
        return total

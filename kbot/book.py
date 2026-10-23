"""Order book for one binary market.

Kalshi publishes only bids for each side. A NO bid at x is a YES ask at 1 - x, and
a YES bid at y is a NO ask at 1 - y. Complementary prices are *derived*, so the
executable price to buy N contracts comes from walking the opposite side's bids.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


def _f(x) -> float:
    return float(x) if x is not None else 0.0


@dataclass
class OrderBook:
    ticker: str
    yes_bids: dict[float, float] = field(default_factory=dict)   # price -> size
    no_bids: dict[float, float] = field(default_factory=dict)
    updated_at: float = 0.0       # local receive time of the last change
    seq: int | None = None

    @classmethod
    def from_rest(cls, ticker: str, ob: dict, ts: float | None = None) -> "OrderBook":
        b = cls(ticker)
        b.yes_bids = {round(_f(p), 4): _f(c) for p, c in ob.get("yes_dollars") or []}
        b.no_bids = {round(_f(p), 4): _f(c) for p, c in ob.get("no_dollars") or []}
        b.updated_at = ts if ts is not None else time.time()
        return b

    def apply_snapshot(self, msg: dict, ts: float | None = None) -> None:
        self.yes_bids = {round(_f(p), 4): _f(c) for p, c in msg.get("yes_dollars_fp") or []}
        self.no_bids = {round(_f(p), 4): _f(c) for p, c in msg.get("no_dollars_fp") or []}
        self.updated_at = ts if ts is not None else time.time()

    def apply_delta(self, msg: dict, ts: float | None = None) -> None:
        side = self.yes_bids if msg["side"] == "yes" else self.no_bids
        price = round(_f(msg["price_dollars"]), 4)
        size = side.get(price, 0.0) + _f(msg["delta_fp"])
        if size <= 1e-9:
            side.pop(price, None)
        else:
            side[price] = size
        self.updated_at = ts if ts is not None else time.time()

    # --- top of book --------------------------------------------------------
    def best_bid(self, outcome: str) -> tuple[float, float] | None:
        bids = self.yes_bids if outcome == "yes" else self.no_bids
        if not bids:
            return None
        p = max(bids)
        return p, bids[p]

    def best_ask(self, outcome: str) -> tuple[float, float] | None:
        opp = self.best_bid("no" if outcome == "yes" else "yes")
        return (round(1 - opp[0], 4), opp[1]) if opp else None

    def asks(self, outcome: str) -> list[tuple[float, float]]:
        """Ask ladder for buying `outcome`, cheapest first."""
        opp = self.no_bids if outcome == "yes" else self.yes_bids
        return [(round(1 - p, 4), s) for p, s in sorted(opp.items(), reverse=True)]

    def spread(self, outcome: str = "yes") -> float | None:
        bid, ask = self.best_bid(outcome), self.best_ask(outcome)
        return round(ask[0] - bid[0], 4) if bid and ask else None

    def mid_yes(self) -> float | None:
        bid, ask = self.best_bid("yes"), self.best_ask("yes")
        if bid and ask:
            return (bid[0] + ask[0]) / 2
        return None

    def fill_for(self, outcome: str, count: float, limit: float) -> tuple[float, float]:
        """Contracts and average price we could take right now at or under `limit`."""
        filled = cost = 0.0
        for price, size in self.asks(outcome):
            if price > limit + 1e-9 or filled >= count:
                break
            take = min(size, count - filled)
            filled += take
            cost += take * price
        return filled, (cost / filled if filled else 0.0)

    def depth_under(self, outcome: str, limit: float) -> float:
        return sum(s for p, s in self.asks(outcome) if p <= limit + 1e-9)

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.updated_at

    def to_json(self, levels: int = 5) -> dict:
        return {
            "yes": sorted(self.yes_bids.items(), reverse=True)[:levels],
            "no": sorted(self.no_bids.items(), reverse=True)[:levels],
        }

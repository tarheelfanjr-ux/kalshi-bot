"""Strategy interface and candidate strategies.

A strategy only *proposes* trades. It never talks to an exchange. Every candidate goes
through the risk manager and then the execution engine, both deterministic.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .book import OrderBook
from .config import StrategyConfig
from .discovery import MarketInfo
from .fees import fee
from .model import prob_with_uncertainty


@dataclass
class Snapshot:
    """Everything a strategy may look at. Built only from data available at `now`."""
    now: float
    market: MarketInfo
    book: OrderBook
    spot: float | None = None
    spot_ts: float | None = None
    sigma: float | None = None            # per sqrt(second)
    known_avg: float | None = None        # partial 60 s average, inside the final minute
    mid_history: list[tuple[float, float]] = field(default_factory=list)   # (ts, yes mid)
    spot_points: list[tuple[float, float]] = field(default_factory=list)   # stand-in price ticks (gold_token)


@dataclass
class Candidate:
    ticker: str
    expiration_ts: float
    outcome_side: str               # "yes" or "no"
    entry_price: float              # executable average price for `count`
    max_entry_price: float          # never pay more than this
    q_win: float                    # estimated P(this side settles $1)
    market_q: float                 # market-implied P(this side), from the mid
    gross_edge: float               # per contract: q_win - entry_price
    fee_per_contract: float
    net_edge: float                 # gross - fee - model uncertainty
    uncertainty: float
    available: float                # contracts available at or under max_entry_price
    count: float                    # proposed size (risk manager may cut it)
    signal_strength: float
    strategy: str
    reason: str
    invalidation: list[str]

    def to_dict(self):
        return asdict(self)


@dataclass
class Rejection:
    ticker: str
    strategy: str
    reason: str


class Strategy(Protocol):
    name: str

    def evaluate(self, snap: Snapshot) -> Candidate | Rejection: ...


def common_filters(snap: Snapshot, cfg: StrategyConfig) -> str | None:
    left = snap.market.seconds_left(snap.now)
    if left < cfg.min_seconds_left:
        return f"{left:.0f}s left, below {cfg.min_seconds_left}"
    if left > cfg.max_seconds_left:
        return f"{left:.0f}s left, above {cfg.max_seconds_left}"
    spread = snap.book.spread("yes")
    if spread is None:
        return "one-sided or empty book"
    if spread > cfg.max_spread:
        return f"spread {spread:.3f} > {cfg.max_spread}"
    return None


def build_candidate(snap: Snapshot, cfg: StrategyConfig, strategy: str, outcome: str,
                    q_yes: float, uncertainty: float, reason: str,
                    max_count: float = 1e9) -> Candidate | Rejection:
    """Turn a probability into a costed, executable candidate for one side."""
    m, book = snap.market, snap.book
    q_win = q_yes if outcome == "yes" else 1 - q_yes
    top = book.best_ask(outcome)
    if top is None:
        return Rejection(m.ticker, strategy, f"no {outcome} ask")
    if top[1] < cfg.min_top_size:
        return Rejection(m.ticker, strategy, f"top size {top[1]:.0f} < {cfg.min_top_size}")
    def clears(p):
        return q_win - p - fee(p, 1, fee_type=m.fee_type, multiplier=m.fee_multiplier) - uncertainty \
            >= cfg.min_net_edge
    if not clears(top[0]):
        return Rejection(m.ticker, strategy, f"{outcome} ask {top[0]:.2f} too high for q={q_win:.3f}")
    # Highest whole-cent price that still clears the minimum net edge after taker fees.
    max_price = top[0]
    while max_price + 0.01 < 1 and clears(round(max_price + 0.01, 2)):
        max_price = round(max_price + 0.01, 2)
    available = book.depth_under(outcome, max_price)
    count = max(0.0, min(max_count, available))
    filled, avg = book.fill_for(outcome, max(count, 1), max_price)
    price = avg or top[0]
    fee_pc = fee(price, max(count, 1), fee_type=m.fee_type, multiplier=m.fee_multiplier) / max(count, 1)
    gross = q_win - price
    net = gross - fee_pc - uncertainty
    if net < cfg.min_net_edge:
        return Rejection(m.ticker, strategy, f"net edge {net:.3f} < {cfg.min_net_edge}")
    mid = book.mid_yes()
    market_q = (mid if outcome == "yes" else 1 - mid) if mid is not None else price
    return Candidate(
        ticker=m.ticker, expiration_ts=m.close_ts, outcome_side=outcome, entry_price=round(price, 4),
        max_entry_price=max_price, q_win=round(q_win, 4), market_q=round(market_q, 4),
        gross_edge=round(gross, 4), fee_per_contract=round(fee_pc, 4), net_edge=round(net, 4),
        uncertainty=round(uncertainty, 4), available=available, count=count,
        signal_strength=round(net / max(uncertainty, 0.01), 3), strategy=strategy, reason=reason,
        invalidation=[
            f"{outcome} ask rises above {max_price:.2f}",
            f"fewer than {cfg.min_seconds_left}s left",
            "market data older than max_data_age_s",
            "market status leaves active",
        ],
    )


class NoTrade:
    name = "no_trade"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def evaluate(self, snap):
        return Rejection(snap.market.ticker, self.name, "benchmark never trades")


class FairValue:
    """Model probability from the underlying index vs. executable market price."""
    name = "fair_value"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def evaluate(self, snap: Snapshot):
        m = snap.market
        why = common_filters(snap, self.cfg)
        if why:
            return Rejection(m.ticker, self.name, why)
        if snap.spot is None or snap.sigma is None or m.floor_strike is None:
            return Rejection(m.ticker, self.name, "missing spot, volatility, or strike")
        if snap.spot_ts is not None and snap.now - snap.spot_ts > 5:
            return Rejection(m.ticker, self.name, f"spot is {snap.now - snap.spot_ts:.1f}s old")
        tau = m.seconds_left(snap.now)
        q, unc = prob_with_uncertainty(snap.spot, m.floor_strike, tau, snap.sigma, snap.known_avg)
        unc = max(unc, self.cfg.model_uncertainty)
        best: Candidate | Rejection | None = None
        for side in ("yes", "no"):
            c = build_candidate(snap, self.cfg, self.name, side, q, unc,
                                f"model P(yes)={q:.3f} spot={snap.spot:.2f} strike={m.floor_strike:.2f} "
                                f"tau={tau:.0f}s sigma={snap.sigma:.2e}")
            if isinstance(c, Candidate) and (not isinstance(best, Candidate) or c.net_edge > best.net_edge):
                best = c
            elif best is None:
                best = c
        return best


class MidMove:
    """Momentum (sign=+1) or mean reversion (sign=-1) on the market's own mid price.

    Probability estimate: market mid shifted by `strength` times the recent move. This
    is a benchmark. It only wins if past mid moves predict outcome beyond the mid.
    """

    def __init__(self, cfg: StrategyConfig, sign: int, lookback_s: float = 180, strength: float = 0.5):
        self.cfg, self.sign, self.lookback, self.strength = cfg, sign, lookback_s, strength
        self.name = "momentum" if sign > 0 else "mean_reversion"

    def evaluate(self, snap: Snapshot):
        m = snap.market
        why = common_filters(snap, self.cfg)
        if why:
            return Rejection(m.ticker, self.name, why)
        hist = [(t, v) for t, v in snap.mid_history if t <= snap.now]
        past = [v for t, v in hist if t <= snap.now - self.lookback]
        mid = snap.book.mid_yes()
        if not past or mid is None:
            return Rejection(m.ticker, self.name, "not enough mid history")
        move = mid - past[-1]
        q = min(0.99, max(0.01, mid + self.sign * self.strength * move))
        side = "yes" if q > mid else "no"
        if abs(move) < 0.02:
            return Rejection(m.ticker, self.name, f"move {move:+.3f} too small")
        return build_candidate(snap, self.cfg, self.name, side, q, self.cfg.model_uncertainty,
                               f"mid moved {move:+.3f} over {self.lookback:.0f}s")


class MarketImplied:
    """Uses the market mid as its probability. After spread and fees it should never trade.
    It exists to prove the cost model is not handing out free edge."""
    name = "market_implied"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def evaluate(self, snap: Snapshot):
        mid = snap.book.mid_yes()
        why = common_filters(snap, self.cfg)
        if why or mid is None:
            return Rejection(snap.market.ticker, self.name, why or "no mid")
        best = None
        for side in ("yes", "no"):
            c = build_candidate(snap, self.cfg, self.name, side, mid, 0.0, "market mid")
            best = c if isinstance(c, Candidate) else (best or c)
        return best


def make(name: str, cfg: StrategyConfig) -> Strategy:
    from .gold import GoldToken
    return {
        "gold_token": lambda: GoldToken(cfg),
        "no_trade": lambda: NoTrade(cfg),
        "fair_value": lambda: FairValue(cfg),
        "momentum": lambda: MidMove(cfg, +1),
        "mean_reversion": lambda: MidMove(cfg, -1),
        "market_implied": lambda: MarketImplied(cfg),
    }[name]()


def contracts_within(max_loss: float, price: float, fee_per_contract: float) -> int:
    per = price + fee_per_contract
    return int(math.floor(max_loss / per + 1e-9)) if per > 0 else 0

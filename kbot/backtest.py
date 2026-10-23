"""Event-driven backtester on Kalshi's per-minute candles.

The same Strategy, RiskManager, and OrderManager classes used live are used here, on an
in-memory database, so a backtest exercises the real risk limits and accounting.

Data honesty rules:
* A decision at time t sees only candles with end_ts <= t and spot points with ts <= t.
* Entries are IOC taker buys at the ask at t plus `slippage`, filled only with probability
  `fill_prob`, and at most `fill_fraction * assumed_depth` contracts (candles carry no depth).
* Positions are held to settlement and paid from the market's official `result`.
* Markets settling at the same moment are one cluster for confidence intervals.
"""
from __future__ import annotations

import asyncio
import bisect
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field, replace

from .book import OrderBook
from .config import RiskLimits, StrategyConfig
from .db import DB, utc_day
from .discovery import MarketInfo
from .execution import ExchangeOrder, OrderManager
from .fees import fee
from .model import realized_sigma
from .risk import RiskManager
from .strategy import Candidate, Snapshot, make


@dataclass
class Candle:
    end_ts: float
    yes_bid: float | None
    yes_ask: float | None


@dataclass
class MarketData:
    info: MarketInfo
    candles: list[Candle]
    result: str               # "yes" | "no"


@dataclass
class Costs:
    slippage: float = 0.01        # added to the ask on every entry
    fill_prob: float = 0.8        # chance an IOC order finds the quote still there
    fill_fraction: float = 0.5
    assumed_depth: float = 10     # contracts at the top level
    fee_multiplier: float = 1.0   # stress test: scale all fees
    settle_delay_s: float = 60


@dataclass
class Trade:
    ticker: str
    series: str
    side: str
    entry_ts: float
    close_ts: float
    price: float
    count: float
    fees: float
    q_win: float
    sigma: float | None
    pnl: float = 0.0
    result: str = ""


@dataclass
class Result:
    trades: list[Trade]
    starting_capital: float
    rejections: dict[str, int] = field(default_factory=dict)
    trips: list[str] = field(default_factory=list)
    decisions: int = 0


class _BacktestAdapter:
    mode = "backtest"

    def __init__(self, costs: Costs, rng: random.Random):
        self.costs, self.rng = costs, rng
        self.book: OrderBook | None = None

    async def submit(self, *, ticker, outcome_side, limit_price, count, client_order_id):
        top = self.book.best_ask(outcome_side) if self.book else None
        if top is None or self.rng.random() > self.costs.fill_prob:
            return ExchangeOrder(None, "canceled", 0, 0, None, 0)
        price = round(top[0] + self.costs.slippage, 4)
        if price > limit_price + 1e-9 or price >= 1:
            return ExchangeOrder(None, "canceled", 0, 0, None, 0)
        n = min(count, math.floor(self.costs.assumed_depth * self.costs.fill_fraction))
        if n <= 0:
            return ExchangeOrder(None, "canceled", 0, 0, None, 0)
        f = fee(price, n) * self.costs.fee_multiplier
        return ExchangeOrder("bt-" + client_order_id[:8], "executed", n, 0, price, f)

    async def cancel(self, ticker, exchange_order_id):
        return None

    async def lookup(self, ticker, client_order_id, exchange_order_id):
        return None

    async def positions(self):
        return None


def _book_from_candle(ticker: str, c: Candle, depth: float, ts: float) -> OrderBook:
    b = OrderBook(ticker, updated_at=ts)
    if c.yes_bid and 0 < c.yes_bid < 1:
        b.yes_bids = {c.yes_bid: depth}
    if c.yes_ask and 0 < c.yes_ask < 1:
        b.no_bids = {round(1 - c.yes_ask, 4): depth}
    return b


def run(markets: list[MarketData], strategy_name: str, scfg: StrategyConfig, limits: RiskLimits,
        costs: Costs | None = None, spot: dict[str, list[tuple[float, float]]] | None = None,
        index_for_series: dict[str, str] | None = None, seed: int = 7,
        side_override=None) -> Result:
    """side_override(trade_index, rng) -> 'yes'|'no' turns a run into a random-direction benchmark."""
    return asyncio.run(_run(markets, strategy_name, scfg, limits, costs or Costs(), spot or {},
                            index_for_series or {}, seed, side_override))


async def _run(markets, strategy_name, scfg, limits, costs, spot, index_for_series, seed, side_override):
    rng = random.Random(seed)
    db = DB(":memory:")
    now = [0.0]
    risk = RiskManager(db, limits, "backtest", clock=lambda: now[0])
    adapter = _BacktestAdapter(costs, rng)
    om = OrderManager(db, adapter)
    strat = make(strategy_name, scfg)
    res = Result([], limits.bankroll)
    rej: dict[str, int] = defaultdict(int)

    spot = {k: sorted(v) for k, v in spot.items()}
    spot_ts = {k: [p[0] for p in v] for k, v in spot.items()}
    events = []   # (ts, order, kind, market_data, candle_index)
    for md in markets:
        db.x("INSERT OR REPLACE INTO markets(ticker,series,close_ts) VALUES(?,?,?)",
             (md.info.ticker, md.info.series, md.info.close_ts))
        for i, c in enumerate(md.candles):
            if md.info.open_ts <= c.end_ts < md.info.close_ts:
                events.append((c.end_ts, 1, "decide", md, i))
        events.append((md.info.close_ts + costs.settle_delay_s, 0, "settle", md, -1))
    events.sort(key=lambda e: (e[0], e[1]))

    open_trades: dict[str, Trade] = {}
    last_day = None
    for ts, _, kind, md, i in events:
        now[0] = ts
        day = utc_day(ts)
        if day != last_day:
            # Simulated daily review: a halt from the previous day is cleared at UTC midnight.
            if risk.halted():
                risk.clear("backtest-new-day")
            last_day = day
        if kind == "settle":
            om.settle(md.info.ticker, md.result, ts)
            t = open_trades.pop(md.info.ticker, None)
            if t:
                row = db.one("SELECT pnl FROM positions WHERE mode='backtest' AND ticker=?", (t.ticker,))
                t.pnl, t.result = row["pnl"], md.result
                res.trades.append(t)
            continue
        res.decisions += 1
        c = md.candles[i]
        book = _book_from_candle(md.info.ticker, c, costs.assumed_depth, ts)
        if book.mid_yes() is None:
            rej["missing quote"] += 1
            continue
        hist = [(x.end_ts, (x.yes_bid + x.yes_ask) / 2) for x in md.candles[: i + 1]
                if x.yes_bid and x.yes_ask]
        snap = Snapshot(now=ts, market=md.info, book=book, mid_history=hist)
        idx = index_for_series.get(md.info.series)
        if idx and idx in spot:
            k = bisect.bisect_right(spot_ts[idx], ts)
            pts = spot[idx][max(0, k - 200):k]
            if pts:
                snap.spot, snap.spot_ts = pts[-1][1], pts[-1][0]
                snap.sigma = realized_sigma(pts, scfg.vol_lookback_s, now=ts, min_points=10)
        cand = strat.evaluate(snap)
        if not isinstance(cand, Candidate):
            rej[cand.reason.split(" ")[0] if cand.reason else "rejected"] += 1
            continue
        if side_override is not None:
            side = side_override(len(open_trades) + len(res.trades), rng)
            if side != cand.outcome_side:
                ask = book.best_ask(side)
                if ask is None:
                    continue
                cand = replace(cand, outcome_side=side, entry_price=ask[0],
                               max_entry_price=min(0.99, ask[0] + costs.slippage), q_win=0.5)
        decision = risk.approve(cand)
        if decision.trip:
            res.trips.append(f"{day}: {decision.trip}")
        if not decision.approved:
            rej["risk: " + decision.reason.split(" (")[0][:40]] += 1
            continue
        adapter.book = book
        coid = await om.place(ticker=cand.ticker, outcome_side=cand.outcome_side,
                              limit_price=cand.max_entry_price, count=decision.count)
        o = db.one("SELECT * FROM orders WHERE client_order_id=?", (coid,))
        if o["filled"] > 0:
            open_trades[cand.ticker] = Trade(cand.ticker, md.info.series, cand.outcome_side, ts,
                                             md.info.close_ts, o["avg_price"], o["filled"], o["fees"],
                                             cand.q_win, snap.sigma)
        else:
            rej["unfilled"] += 1
    res.rejections = dict(rej)
    db.close()
    return res


# --- metrics -----------------------------------------------------------------

def _max_drawdown(pnls: list[float], start: float) -> float:
    eq = peak = start
    dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def cluster_bootstrap_ci(trades: list[Trade], n_boot: int = 2000, seed: int = 1,
                         stat=lambda xs: sum(xs) / len(xs)) -> tuple[float, float] | None:
    """95% CI of mean pnl per trade, resampling whole settlement-time clusters."""
    clusters: dict[float, list[float]] = defaultdict(list)
    for t in trades:
        clusters[t.close_ts].append(t.pnl)
    keys = list(clusters)
    if len(keys) < 5:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        xs = [p for k in (rng.choice(keys) for _ in keys) for p in clusters[k]]
        stats.append(stat(xs))
    stats.sort()
    return stats[int(0.025 * n_boot)], stats[int(0.975 * n_boot) - 1]


def metrics(res: Result) -> dict:
    tr = sorted(res.trades, key=lambda t: t.close_ts)
    n = len(tr)
    out = {"trades": n, "decisions": res.decisions, "independent_windows": len({t.close_ts for t in tr}),
           "rejections": res.rejections, "risk_trips": res.trips}
    if not n:
        return out
    pnls = [t.pnl for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    deployed = sum(t.price * t.count + t.fees for t in tr)
    fees = sum(t.fees for t in tr)
    gross = sum(t.pnl + t.fees for t in tr)
    streak = best = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        best = max(best, streak)
    daily: dict[str, float] = defaultdict(float)
    for t in tr:
        daily[utc_day(t.close_ts)] += t.pnl
    d = list(daily.values())
    sharpe = None
    if len(d) > 1:
        mu = sum(d) / len(d)
        sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (len(d) - 1))
        sharpe = mu / sd * math.sqrt(365) if sd > 0 else None
    by_series: dict[str, list[float]] = defaultdict(list)
    for t in tr:
        by_series[t.series].append(t.pnl)
    sig = sorted(t.sigma for t in tr if t.sigma is not None)
    by_regime = {}
    if len(sig) >= 9:
        lo, hi = sig[len(sig) // 3], sig[2 * len(sig) // 3]
        for t in tr:
            if t.sigma is None:
                continue
            k = "low_vol" if t.sigma < lo else "high_vol" if t.sigma >= hi else "mid_vol"
            by_regime.setdefault(k, []).append(t.pnl)
    ci = cluster_bootstrap_ci(tr)
    out.update({
        "total_net_pnl": round(sum(pnls), 4),
        "return_on_start_capital": round(sum(pnls) / res.starting_capital, 4),
        "return_on_deployed": round(sum(pnls) / deployed, 4) if deployed else None,
        "max_drawdown": round(_max_drawdown(pnls, res.starting_capital), 4),
        "sharpe_daily_annualized": round(sharpe, 2) if sharpe is not None else None,
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "avg_net_per_trade": round(sum(pnls) / n, 4),
        "ci95_avg_net_per_trade": [round(x, 4) for x in ci] if ci else None,
        "longest_losing_streak": best,
        "total_fees": round(fees, 4),
        "fee_burden_of_gross": round(fees / gross, 3) if gross > 0 else None,
        "avg_predicted_edge": round(sum(t.q_win - t.price - t.fees / t.count for t in tr) / n, 4),
        "by_series": {k: {"n": len(v), "pnl": round(sum(v), 4)} for k, v in sorted(by_series.items())},
        "by_vol_regime": {k: {"n": len(v), "pnl": round(sum(v), 4)} for k, v in by_regime.items()},
        "trading_days": len(d),
    })
    return out


def chronological_split(markets: list[MarketData], train=0.6, valid=0.2):
    ms = sorted(markets, key=lambda m: m.info.close_ts)
    a, b = int(len(ms) * train), int(len(ms) * (train + valid))
    return ms[:a], ms[a:b], ms[b:]


def walk_forward(markets: list[MarketData], folds: int = 4) -> list[list[MarketData]]:
    """Consecutive, non-overlapping test blocks in time order. The strategies here have no
    fitted parameters, so each block is an out-of-sample test of fixed rules."""
    ms = sorted(markets, key=lambda m: m.info.close_ts)
    size = max(1, len(ms) // folds)
    return [ms[i * size:(i + 1) * size if i < folds - 1 else len(ms)] for i in range(folds)]


def random_baseline(markets, strategy_name, scfg, limits, costs, spot, idx, n_runs=100):
    """Same strategy timing and sizing, coin-flip direction. Returns total pnl per run."""
    return [sum(t.pnl for t in run(markets, strategy_name, scfg, limits, costs, spot, idx, seed=s,
                                   side_override=lambda i, r: r.choice(("yes", "no"))).trades)
            for s in range(n_runs)]

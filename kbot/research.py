"""Strategy research report: calibration of market prices, strategy comparison,
benchmarks, cost sensitivity, and an untouched holdout that is opened only on request.

Output is labelled HISTORICAL BACKTEST everywhere. It never mixes with paper or live data.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from .backtest import (Costs, MarketData, chronological_split, metrics, random_baseline, run,
                       walk_forward)
from .config import Settings
from .db import DB
from .feed import UNDERLYING
from .fees import fee

CANDIDATES = ["fair_value", "momentum", "mean_reversion"]
BENCHMARKS = ["no_trade", "market_implied"]


def calibration(markets: list[MarketData], minutes_left=(10, 5, 2)) -> dict:
    """Does the market mid predict outcomes? And what does buying the ask actually earn?"""
    out = {}
    for ml in minutes_left:
        buckets: dict[int, list] = defaultdict(list)
        brier = []
        for md in markets:
            target = md.info.close_ts - ml * 60
            c = min((c for c in md.candles if c.end_ts <= target and c.yes_bid and c.yes_ask),
                    key=lambda c: target - c.end_ts, default=None)
            if c is None or target - c.end_ts > 120:
                continue
            mid = (c.yes_bid + c.yes_ask) / 2
            y = 1.0 if md.result == "yes" else 0.0
            brier.append((mid - y) ** 2)
            # Net return of buying YES at the ask and NO at the NO ask (1 - yes_bid), per contract.
            yes_ret = y - c.yes_ask - fee(c.yes_ask, 1) if 0 < c.yes_ask < 1 else None
            no_ask = 1 - c.yes_bid
            no_ret = (1 - y) - no_ask - fee(no_ask, 1) if 0 < no_ask < 1 else None
            buckets[min(9, int(mid * 10))].append((mid, y, yes_ret, no_ret, c.yes_ask - c.yes_bid))
        out[f"{ml}min_left"] = {
            "n": len(brier),
            "brier": round(sum(brier) / len(brier), 4) if brier else None,
            "buckets": {
                f"{b / 10:.1f}-{(b + 1) / 10:.1f}": {
                    "n": len(v),
                    "avg_mid": round(sum(x[0] for x in v) / len(v), 3),
                    "yes_rate": round(sum(x[1] for x in v) / len(v), 3),
                    "avg_spread": round(sum(x[4] for x in v) / len(v), 3),
                    "buy_yes_net": _avg([x[2] for x in v]),
                    "buy_no_net": _avg([x[3] for x in v]),
                } for b, v in sorted(buckets.items())
            },
        }
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def compare(markets, s: Settings, spot, idx, costs: Costs, label: str, baseline_runs: int = 0) -> dict:
    rows = {}
    for name in CANDIDATES + BENCHMARKS:
        cfg = replace(s.strategy, name=name)
        r = run(markets, name, cfg, s.risk, costs, spot, idx)
        rows[name] = metrics(r)
        if baseline_runs and name in CANDIDATES and r.trades:
            base = random_baseline(markets, name, cfg, s.risk, costs, spot, idx, n_runs=baseline_runs)
            total = sum(t.pnl for t in r.trades)
            rows[name]["random_direction_baseline"] = {
                "runs": len(base), "mean_total": round(sum(base) / len(base), 4),
                "share_of_random_runs_beaten": round(sum(b < total for b in base) / len(base), 3)}
    return {"label": label, "markets": len(markets), "results": rows}


def sensitivity(markets, s: Settings, spot, idx, name: str) -> dict:
    cfg = replace(s.strategy, name=name)
    out = {}
    for label, c in {
        "base": Costs(),
        "slippage_2c": Costs(slippage=0.02),
        "slippage_3c": Costs(slippage=0.03),
        "fill_prob_50": Costs(fill_prob=0.5),
        "fees_x1.5": Costs(fee_multiplier=1.5),
        "worst_combo": Costs(slippage=0.03, fill_prob=0.5, fee_multiplier=1.5),
    }.items():
        m = metrics(run(markets, name, cfg, s.risk, c, spot, idx))
        out[label] = {k: m.get(k) for k in ("trades", "total_net_pnl", "avg_net_per_trade",
                                            "ci95_avg_net_per_trade", "max_drawdown")}
    return out


def research(db: DB, s: Settings, open_holdout: bool = False, out_dir: str = "reports") -> dict:
    from .collector import load_markets, load_spot

    markets = load_markets(db, s.series or None)
    spot = load_spot(db)
    idx = {k: v[0] for k, v in UNDERLYING.items()}
    rep: dict = {
        "label": "HISTORICAL BACKTEST (simulated fills on Kalshi minute candles)",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markets_total": len(markets),
        "series": sorted({m.info.series for m in markets}),
        "spot_indices": {k: len(v) for k, v in spot.items()},
        "independent_windows": len({m.info.close_ts for m in markets}),
    }
    if len(markets) < 50:
        rep["verdict"] = "INSUFFICIENT DATA: fewer than 50 settled markets with candles. No conclusions."
        _write(rep, out_dir)
        return rep
    train, valid, hold = chronological_split(markets)
    rep["split"] = {"train": len(train), "validation": len(valid), "holdout": len(hold),
                    "holdout_start_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(hold[0].info.close_ts))}
    rep["calibration_train"] = calibration(train)
    rep["train"] = compare(train, s, spot, idx, Costs(), "train (in-sample for rule design)")
    rep["validation"] = compare(valid, s, spot, idx, Costs(), "validation (out of sample)", baseline_runs=20)
    rep["walk_forward_fair_value"] = [
        {k: metrics(run(f, "fair_value", replace(s.strategy, name="fair_value"), s.risk, Costs(), spot, idx)).get(k)
         for k in ("trades", "total_net_pnl", "avg_net_per_trade", "ci95_avg_net_per_trade")}
        for f in walk_forward(train + valid)]
    rep["sensitivity_validation"] = {n: sensitivity(valid, s, spot, idx, n) for n in CANDIDATES}
    opened = db.get("holdout_opened_count", 0)
    if open_holdout:
        db.set("holdout_opened_count", opened + 1)
        db.event("holdout_opened", n=opened + 1)
        rep["holdout"] = compare(hold, s, spot, idx, Costs(), "FINAL HOLDOUT", baseline_runs=20)
        rep["holdout_opened_count"] = opened + 1
        if opened:
            rep["holdout_warning"] = (f"holdout has been opened {opened + 1} times; results after the first "
                                      "look are contaminated by any tuning done in between")
    rep["verdict"] = verdict(rep)
    _write(rep, out_dir)
    return rep


def verdict(rep: dict) -> str:
    """A strategy passes only if validation shows positive mean net pnl per trade whose
    cluster-bootstrap 95% CI excludes zero, with at least 100 trades, it beats the random
    baseline in >= 95% of runs, and it stays positive with 2c extra slippage."""
    passed = []
    for name in CANDIDATES:
        m = rep["validation"]["results"].get(name, {})
        ci = m.get("ci95_avg_net_per_trade")
        rb = (m.get("random_direction_baseline") or {}).get("share_of_random_runs_beaten", 0)
        sens = rep["sensitivity_validation"][name]["slippage_2c"]
        if (m.get("trades", 0) >= 100 and ci and ci[0] > 0 and rb >= 0.95
                and (sens.get("total_net_pnl") or 0) > 0):
            passed.append(name)
    if not passed:
        return "NO STRATEGY PASSED validation criteria. Do not trade real money. Keep collecting data."
    return f"PASSED validation: {', '.join(passed)}. Next: open the holdout once, then paper trade."


def _write(rep: dict, out_dir: str) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    Path(out_dir, f"research-{stamp}.json").write_text(json.dumps(rep, indent=2, default=str))
    Path(out_dir, "research-latest.json").write_text(json.dumps(rep, indent=2, default=str))

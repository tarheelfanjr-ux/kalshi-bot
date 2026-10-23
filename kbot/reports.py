"""Performance and calibration from the operating database, per mode.

Mode labels: paper = SIMULATED PAPER TRADING, demo = KALSHI DEMO ACCOUNT (not real money),
production = REAL MONEY. These are never summed together.
"""
from __future__ import annotations

import math
import time

from .db import DB, utc_day

LABELS = {
    "paper": "SIMULATED PAPER TRADING (live data, simulated fills)",
    "demo": "KALSHI DEMO ACCOUNT (exchange-simulated, not real money)",
    "production": "REAL MONEY PRODUCTION",
    "backtest": "HISTORICAL BACKTEST",
}


def performance(db: DB, mode: str, bankroll: float) -> dict:
    rows = db.q("SELECT * FROM positions WHERE mode=? AND status='settled' ORDER BY settled_at", (mode,))
    pnls = [r["pnl"] for r in rows]
    fees = db.one("SELECT COALESCE(SUM(fee),0) f FROM fills WHERE mode=?", (mode,))["f"]
    eq, peak, dd, curve = bankroll, bankroll, 0.0, []
    for r in rows:
        eq += r["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        curve.append((r["settled_at"], round(eq, 4)))
    wins = [p for p in pnls if p > 0]
    return {
        "label": LABELS.get(mode, mode),
        "settled_trades": len(pnls),
        "net_pnl": round(sum(pnls), 4),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "avg_net_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "max_drawdown": round(dd, 4),
        "fees": round(fees, 4),
        "daily_realized": round(db.daily_realized(mode, utc_day()), 4),
        "equity_curve": curve[-500:],
    }


def calibration(db: DB, mode: str) -> dict:
    """Predicted win probability at entry vs realized outcome for settled trades."""
    rows = db.q(
        "SELECT s.q_win, p.pnl FROM positions p JOIN orders o ON o.ticker=p.ticker AND o.mode=p.mode "
        "AND o.outcome_side=p.outcome_side JOIN signals s ON s.id=o.signal_id "
        "WHERE p.mode=? AND p.status='settled' AND o.filled>0", (mode,))
    if not rows:
        return {"n": 0}
    pairs = [(r["q_win"], 1.0 if r["pnl"] > 0 else 0.0) for r in rows]
    brier = sum((q - y) ** 2 for q, y in pairs) / len(pairs)
    pred, real = sum(q for q, _ in pairs) / len(pairs), sum(y for _, y in pairs) / len(pairs)
    se = math.sqrt(max(real * (1 - real), 1e-9) / len(pairs))
    return {"n": len(pairs), "brier": round(brier, 4), "avg_predicted": round(pred, 4),
            "realized_win_rate": round(real, 4), "gap_in_std_errors": round((real - pred) / se, 2)}


def deterioration(db: DB, mode: str, window: int = 50) -> str | None:
    """Flag if the last `window` trades lost money on average while predicting an edge."""
    rows = db.q("SELECT pnl FROM positions WHERE mode=? AND status='settled' ORDER BY settled_at DESC LIMIT ?",
                (mode, window))
    if len(rows) < window:
        return None
    avg = sum(r["pnl"] for r in rows) / len(rows)
    return f"last {window} trades averaged {avg:.4f} per trade" if avg < 0 else None


def daily_report(db: DB, mode: str, bankroll: float) -> str:
    p = performance(db, mode, bankroll)
    c = calibration(db, mode)
    rej = db.q("SELECT reason, COUNT(*) n FROM signals WHERE mode=? AND accepted=0 AND ts>? "
               "GROUP BY reason ORDER BY n DESC LIMIT 8", (mode, time.time() - 86400))
    lines = [f"# Daily report {utc_day()} | {p['label']}", "",
             f"Settled trades: {p['settled_trades']}  Net P&L: {p['net_pnl']}  Win rate: {p['win_rate']}",
             f"Avg per trade: {p['avg_net_per_trade']}  Max drawdown: {p['max_drawdown']}  Fees: {p['fees']}",
             f"Calibration: {c}", "", "Top rejection reasons (24h):"]
    lines += [f"  {r['n']:6d}  {r['reason']}" for r in rej]
    if (d := deterioration(db, mode)):
        lines += ["", f"WARNING: {d}"]
    return "\n".join(lines)

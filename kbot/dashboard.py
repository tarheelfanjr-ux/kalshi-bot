"""Bot state for `kbot status` and the web control center (kbot/web.py).

It never includes key ids, key paths, or other secrets.
"""
from __future__ import annotations

import time

from .config import Settings
from .db import DB
from .reports import LABELS, calibration, performance
from .risk import RiskManager


def state(db: DB, s: Settings) -> dict:
    running = db.get("running") or {}
    mode = running.get("mode") or s.mode
    risk = RiskManager(db, s.risk, mode)
    L = s.risk
    now = time.time()
    perf = performance(db, mode, L.bankroll)
    eq = db.one("SELECT * FROM equity WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,))
    return {
        "now": now,
        "mode": mode,
        "mode_label": LABELS.get(mode, mode),
        "running": bool(running),
        "heartbeat_age_s": round(now - (db.get("heartbeat") or 0), 1),
        "health": db.get("health"),
        "halted": risk.halted(),
        "production_trading": "ARMED" if mode == "production" else "disabled",
        "account_balance": db.get(f"balance:{mode}") if mode in ("demo", "production") else None,
        "bankroll": L.bankroll,
        "risk_utilization": {
            "open_risk": [round(risk.open_risk(), 2), round(L.max_open_risk_pct * L.bankroll, 2)],
            "daily_loss": [round(risk.daily_loss(), 2), round(L.max_daily_loss_pct * L.bankroll, 2)],
            "open_positions": [len(risk.busy_tickers()), L.max_open_positions],
            "consecutive_losses": [risk.consecutive_losses(), L.max_consecutive_losses],
        },
        "performance": perf,
        "calibration": calibration(db, mode),
        "unrealized": round(eq["open_value"], 4) if eq else None,
        "positions": [dict(r) for r in db.q(
            "SELECT ticker,outcome_side,count,cost,fees,close_ts FROM positions WHERE mode=? AND status='open'", (mode,))],
        "orders": [dict(r) for r in db.q(
            "SELECT client_order_id,ticker,outcome_side,limit_price,count,filled,state,created_at FROM orders "
            "WHERE mode=? AND state NOT IN ('filled','canceled','rejected')", (mode,))],
        "opportunities": [dict(r) for r in db.q(
            "SELECT ts,ticker,outcome_side,entry_price,q_win,market_q,net_edge,accepted,reason FROM signals "
            "WHERE mode=? AND net_edge IS NOT NULL AND ts>? ORDER BY ts DESC LIMIT 20", (mode, now - 600))],
        "recent_rejections": [dict(r) for r in db.q(
            "SELECT ts,ticker,reason FROM signals WHERE mode=? AND accepted=0 ORDER BY ts DESC LIMIT 20", (mode,))],
        "last_reconcile": db.get(f"last_reconcile:{mode}"),
        "reconcile_problems": db.get(f"reconcile_problems:{mode}"),
        "last_data_update": (db.one("SELECT MAX(ts) t FROM snapshots") or {"t": None})["t"],
        "events": [dict(r) for r in db.q(
            "SELECT ts,kind,ticker,data FROM events WHERE kind IN "
            "('error','notify','risk_trip','risk_clear','unexpected_termination','startup','shutdown') "
            "ORDER BY ts DESC LIMIT 30")],
    }

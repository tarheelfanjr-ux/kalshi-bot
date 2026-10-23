"""Web control center: watch and operate the bot from a browser. Standard library only.

It reads the same SQLite database and config file the bot uses, and every control goes through
kbot.control, the same code as the CLI. It can start the bot in paper mode only and has no way to
arm production or place an order.

Security:
* Binds to 127.0.0.1. Any other address needs KBOT_DASHBOARD_TOKEN (16+ characters); then every
  API call needs a session cookie from logging in with that token (HttpOnly, SameSite=Strict).
* Every POST needs a CSRF token header, a JSON body and, when the browser sends one, a same-origin
  Origin. Without a token the Host header must be a local name, which stops DNS rebinding.
* Responses never carry key ids, key paths, the arm phrase, the webhook or the login token.
"""
from __future__ import annotations

import hmac
import inspect
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import tomllib
from dataclasses import fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config as config_mod
from .config import RiskLimits, Settings
from .control import ControlError, bot_state, estop, pause, resume, start_paper, stop_process
from .dashboard import state
from .db import DB
from .experiments import SERIES, SPECS
from .reports import performance
from .strategy import make

STATIC = Path(__file__).with_name("web")
LOCAL = ("127.0.0.1", "localhost", "::1")
SESSION_S = 12 * 3600
QUIET_EVENTS = ("lifecycle", "order_state", "order_submit", "order_cancel", "market_left_open_set", "fee_state",
                "recorder_health")
# Read the names from make() itself, so a strategy added there appears here with no second list to keep.
STRATEGIES = re.findall(r'"(\w+)":\s*lambda', inspect.getsource(make)) + ["maker"]
NOTES = {
    "fair_value": "Retired on 2026-10-23: the edge audit found the model loses to the market mid (docs/REPORT.md).",
    "maker": "Paper only. E2's quoting rule live; real orders stay off until its owner lifts the guard (docs/GO_LIVE.md).",
    "no_trade": "Baseline: never trades, so it never loses.",
    "market_implied": "Benchmark: prices off the market's own mid.",
}


def _clean(x):
    """JSON has no NaN or infinity."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


# --- overview, controls --------------------------------------------------------------------------

def api_state(db: DB, s: Settings, config_path: str) -> dict:
    st = state(db, s)
    mode = st["mode"]
    settled = db.one("SELECT COALESCE(SUM(pnl+fees),0) g, COALESCE(SUM(fees),0) f FROM positions "
                     "WHERE mode=? AND status='settled'", (mode,))
    perf = st["performance"]
    now = time.time()
    running = db.get("running") or {}
    cfg_mtime = Path(config_path).stat().st_mtime if Path(config_path).exists() else 0
    L = s.risk
    budget = L.max_position_risk_pct * L.bankroll
    return {
        "now": now, "mode": mode, "mode_label": st["mode_label"], "config": config_path,
        "production_trading": st["production_trading"], "bot": bot_state(db, s, now),
        "strategy": s.strategy.name, "series": s.series,
        "settings_changed_since_start": bool(running.get("started") and cfg_mtime > running["started"]),
        "pnl": {"label": perf["label"], "trades": perf["settled_trades"], "gross": round(settled["g"], 4),
                "fees": round(settled["f"], 4), "net": perf["net_pnl"], "today": perf["daily_realized"],
                "win_rate": perf["win_rate"], "max_drawdown": perf["max_drawdown"], "unrealized": st["unrealized"],
                "fees_all_fills": perf["fees"], "equity_curve": perf["equity_curve"]},
        "bankroll": L.bankroll,
        "limits": {f.name: getattr(L, f.name) for f in fields(RiskLimits)},
        "max_price_one_contract": round(budget, 2),
        "risk_utilization": st["risk_utilization"],
        "positions": st["positions"], "orders": st["orders"],
        "last_data_update": st["last_data_update"], "last_reconcile": st["last_reconcile"],
        "reconcile_problems": st["reconcile_problems"],
        "active_markets": _rows(db.q("SELECT ticker, series, open_ts, close_ts, floor_strike FROM markets "
                                     "WHERE close_ts>? AND (open_ts IS NULL OR open_ts<=?) ORDER BY close_ts, ticker "
                                     "LIMIT 40", (now, now))),
        "entry_cutoff_s": SPECS["E2"]["quote_until_s_left"] if s.strategy.name == "maker" else s.strategy.min_seconds_left,
        "exposure": round(sum(p["cost"] + p["fees"] for p in st["positions"]), 4),
        "recent_decisions": _rows(db.q("SELECT id, ts, strategy, ticker, outcome_side, accepted, reason FROM signals "
                                       "WHERE mode=? ORDER BY id DESC LIMIT 6", (mode,))),
        "recent_fills": _rows(db.q("SELECT ts, ticker, outcome_side, count, price, fee FROM fills WHERE mode=? "
                                   "ORDER BY ts DESC LIMIT 6", (mode,))),
        "activity": _rows(db.q(f"SELECT ts, kind, ticker, data FROM events WHERE kind NOT IN "
                               f"({','.join('?' * len(QUIET_EVENTS))}) ORDER BY ts DESC LIMIT 25", QUIET_EVENTS)),
    }


def api_markets(db: DB, s: Settings) -> dict:
    now = time.time()
    mode = (db.get("running") or {}).get("mode") or s.mode
    out = []
    for m in db.q("SELECT ticker, series, open_ts, close_ts, floor_strike, status FROM markets WHERE close_ts>? "
                  "ORDER BY close_ts, ticker LIMIT 200", (now,)):
        r = dict(m)
        r["book"] = dict(b) if (b := db.one(
            "SELECT ts, yes_bid, yes_ask, yes_bid_size, yes_ask_size, spot, spot_ts FROM snapshots WHERE ticker=? "
            "ORDER BY ts DESC LIMIT 1", (m["ticker"],))) else None
        r["signal"] = dict(g) if (g := db.one(
            "SELECT id, ts, strategy, outcome_side, entry_price, q_win, market_q, net_edge, accepted, reason "
            "FROM signals WHERE mode=? AND ticker=? ORDER BY id DESC LIMIT 1", (mode, m["ticker"]))) else None
        r["quotes"] = _rows(db.q("SELECT side, price, quote_ts, risk FROM quotes WHERE mode=? AND ticker=? "
                                 "AND status='open' AND end_ts>?", (mode, m["ticker"], now)))
        out.append(r)
    return {"now": now, "mode": mode, "updated": (db.one("SELECT MAX(ts) t FROM snapshots") or {"t": None})["t"],
            "stale_after_s": 30, "markets": out}


# --- positions and trades -----------------------------------------------------------------------

def api_trades(db: DB, s: Settings, q: str = "", view: str = "all", limit: int = 100) -> dict:
    mode = (db.get("running") or {}).get("mode") or s.mode
    like = f"%{q.strip()}%"
    limit = max(1, min(limit, 500))
    pos = _rows(db.q(
        "SELECT p.*, (SELECT yes_bid FROM snapshots WHERE ticker=p.ticker ORDER BY ts DESC LIMIT 1) yes_bid, "
        "(SELECT yes_ask FROM snapshots WHERE ticker=p.ticker ORDER BY ts DESC LIMIT 1) yes_ask, "
        "(SELECT ts FROM snapshots WHERE ticker=p.ticker ORDER BY ts DESC LIMIT 1) mark_ts "
        "FROM positions p WHERE mode=? AND status='open' ORDER BY close_ts", (mode,)))
    for p in pos:
        # marked at the price we could sell at now: the bid on our side
        bid = p["yes_bid"] if p["outcome_side"] == "yes" else (1 - p["yes_ask"] if p["yes_ask"] is not None else None)
        p["mark"] = bid
        p["unrealized"] = round(p["count"] * bid - p["cost"] - p["fees"], 4) if bid is not None else None
    fills = _rows(db.q(
        "SELECT f.fill_id, f.ts, f.ticker, f.outcome_side, f.count, f.price, f.fee, f.is_taker, f.client_order_id, "
        "COALESCE(s.strategy, CASE WHEN q.id IS NOT NULL THEN 'maker' END) strategy, COALESCE(s.reason, q.risk) why, "
        "COALESCE(s.id, (SELECT id FROM signals WHERE mode=f.mode AND ticker=f.ticker AND outcome_side=f.outcome_side "
        " AND accepted=1 AND ts BETWEEN q.quote_ts-5 AND q.quote_ts+5 ORDER BY id DESC LIMIT 1)) signal_id, "
        "p.status position_status, p.result, p.pnl "
        "FROM fills f LEFT JOIN orders o ON o.client_order_id=f.client_order_id "
        "LEFT JOIN signals s ON s.id=o.signal_id LEFT JOIN quotes q ON q.client_order_id=f.client_order_id "
        "LEFT JOIN positions p ON p.mode=f.mode AND p.ticker=f.ticker AND p.outcome_side=f.outcome_side "
        "WHERE f.mode=? AND (f.ticker LIKE ? OR COALESCE(s.reason, q.risk, '') LIKE ?) ORDER BY f.ts DESC LIMIT ?",
        (mode, like, like, limit)))
    accepted = {"taken": " AND accepted=1", "skipped": " AND accepted=0"}.get(view, "")
    decisions = _rows(db.q(
        "SELECT id, ts, strategy, ticker, outcome_side, entry_price, q_win, market_q, net_edge, count, accepted, reason "
        f"FROM signals WHERE mode=?{accepted} AND (ticker LIKE ? OR reason LIKE ? OR strategy LIKE ?) "
        "ORDER BY id DESC LIMIT ?", (mode, like, like, like, limit)))
    settled = _rows(db.q("SELECT ticker, outcome_side, count, cost, fees, result, pnl, opened_at, settled_at "
                         "FROM positions WHERE mode=? AND status='settled' AND ticker LIKE ? "
                         "ORDER BY settled_at DESC LIMIT ?", (mode, like, limit)))
    return {"mode": mode, "positions": pos, "exposure": round(sum(p["cost"] + p["fees"] for p in pos), 4),
            "unrealized": round(sum(p["unrealized"] or 0 for p in pos), 4) if pos else 0.0,
            "unmarked": sum(p["unrealized"] is None for p in pos),
            "fills": fills, "decisions": decisions, "settled": settled, "limit": limit}


def api_decision(db: DB, s: Settings, signal_id: int) -> dict | None:
    sig = db.one("SELECT * FROM signals WHERE id=?", (signal_id,))
    if not sig:
        return None
    sig = dict(sig)
    sig["data"] = json.loads(sig["data"]) if sig.get("data") else None
    orders = _rows(db.q("SELECT * FROM orders WHERE signal_id=?", (signal_id,)))
    quote = None
    if sig["strategy"] == "maker" and sig["outcome_side"]:
        quote = db.one("SELECT * FROM quotes WHERE mode=? AND ticker=? AND side=? AND ABS(price-?)<1e-9 "
                       "AND quote_ts BETWEEN ? AND ? ORDER BY ABS(quote_ts-?) LIMIT 1",
                       (sig["mode"], sig["ticker"], sig["outcome_side"], sig["entry_price"] or 0,
                        sig["ts"] - 5, sig["ts"] + 5, sig["ts"]))
        quote = dict(quote) if quote else None
        if quote and quote["client_order_id"] and not orders:
            orders = _rows(db.q("SELECT * FROM orders WHERE client_order_id=?", (quote["client_order_id"],)))
    coids = [o["client_order_id"] for o in orders]
    fills = _rows(db.q(f"SELECT * FROM fills WHERE client_order_id IN ({','.join('?' * len(coids))})", coids)) \
        if coids else []
    pos = db.one("SELECT * FROM positions WHERE mode=? AND ticker=? AND outcome_side=?",
                 (sig["mode"], sig["ticker"], sig["outcome_side"])) if fills else None
    market = db.one("SELECT ticker, series, open_ts, close_ts, floor_strike, status, result FROM markets WHERE ticker=?",
                    (sig["ticker"],))
    return {"signal": sig, "quote": quote, "orders": orders, "fills": fills, "position": dict(pos) if pos else None,
            "market": dict(market) if market else None, "min_net_edge": s.strategy.min_net_edge}


# --- strategies ---------------------------------------------------------------------------------

def _latest(reports_dir: str, pattern: str) -> tuple[str, dict] | None:
    files = sorted(Path(reports_dir).glob(pattern), key=lambda p: p.stat().st_mtime)
    for p in reversed(files):
        try:
            return p.name, json.loads(p.read_text())
        except (OSError, ValueError):
            continue
    return None


def verdict(n: int, ci) -> dict:
    """Honest wording only. Nothing here ever says profitable."""
    if not n:
        return {"key": "none", "short": "No trades", "text": "No trades yet"}
    if not ci or ci[0] is None:
        return {"key": "thin", "short": "Too few trades", "text": f"Too few trades to judge ({n})"}
    if ci[1] < 0:
        return {"key": "loses", "short": "Loses after fees", "text": "Loses money after fees (95% CI below zero)"}
    if ci[0] > 0:
        return {"key": "unproven", "short": "Not proven",
                "text": "Above zero in this sample only. Not proven; needs fresh data"}
    return {"key": "no-edge", "short": "No edge shown", "text": "No edge shown (95% CI includes zero)"}


def api_strategies(db: DB, s: Settings, reports_dir: str) -> dict:
    research = _latest(reports_dir, "research-*.json")
    maker_hist = _latest(reports_dir, "maker-history*.json")
    mode = (db.get("running") or {}).get("mode") or s.mode
    maker_rep = None
    if s.strategy.name == "maker":
        from .maker import report
        maker_rep = report(db, mode, s.risk.bankroll)
    rows = []
    for name in STRATEGIES:
        r = {"name": name, "configured": name == s.strategy.name, "note": NOTES.get(name),
             "backtestable": name != "maker", "historical": None, "paper": None, "paper_per_contract": None}
        if research and (m := research[1].get("validation", {}).get("results", {}).get(name)):
            n = m.get("trades") or 0
            r["historical"] = {
                "source": f"{research[0]}, validation split ({research[1].get('generated_utc', 'date unknown')})",
                "label": research[1].get("label"), "trades": n, "unit": "per trade",
                "net": m.get("total_net_pnl") if n else 0.0, "fees": m.get("total_fees") if n else 0.0,
                "gross": round(m["total_net_pnl"] + m["total_fees"], 4) if n else 0.0,
                "per_trade": m.get("avg_net_per_trade"), "ci": m.get("ci95_avg_net_per_trade"),
                "max_drawdown": m.get("max_drawdown") if n else 0.0,
                "bankroll": None, "verdict": verdict(n, m.get("ci95_avg_net_per_trade"))}
        if name == "maker" and maker_hist and (p := maker_hist[1].get("primary")):
            net, gross = p.get("net_zero_fee", {}), p.get("gross", {})
            r["historical"] = {
                "source": maker_hist[0], "label": maker_hist[1].get("label"), "trades": net.get("n", 0),
                "unit": "per contract (fills)", "per_trade": net.get("mean"), "gross_per_trade": gross.get("mean"),
                "ci": net.get("ci"), "net": None, "gross": None, "fees": None, "max_drawdown": None,
                "verdict": verdict(net.get("n", 0), net.get("ci"))}
        if name == s.strategy.name:
            perf = performance(db, mode, s.risk.bankroll)
            g = db.one("SELECT COALESCE(SUM(pnl+fees),0) g, COALESCE(SUM(fees),0) f FROM positions WHERE mode=? "
                       "AND status='settled'", (mode,))
            r["paper"] = {"label": perf["label"], "trades": perf["settled_trades"], "gross": round(g["g"], 4),
                          "fees": round(g["f"], 4), "net": perf["net_pnl"], "max_drawdown": perf["max_drawdown"],
                          "per_trade": perf["avg_net_per_trade"], "win_rate": perf["win_rate"]}
        if maker_rep and name in ("maker", "fair_value"):
            part = maker_rep["maker_research_log" if name == "maker" else "fair_value_shadow"]
            net, gross = part["net_per_contract"], part["gross_per_contract"]
            r["paper_per_contract"] = {
                "label": maker_rep["label"], "since_utc": maker_rep["since_utc"], "what": part["what"],
                "n": net.get("n", 0), "windows": net.get("windows"), "gross": gross.get("mean"), "net": net.get("mean"),
                "ci": net.get("ci"), "verdict": verdict(net.get("n", 0), net.get("ci"))}
        rows.append(r)
    return {"strategies": rows, "baseline": {"name": "no_trade", "net": 0.0, "trades": 0},
            "research_verdict": research[1].get("verdict") if research else None,
            "reports_dir": reports_dir, "have_reports": bool(research or maker_hist)}


# --- backtests ----------------------------------------------------------------------------------

SPLITS = {"train": "Train (first 60%)", "validation": "Validation (next 20%)",
          "all_but_holdout": "Train + validation (first 80%)"}


def dataset_info(path: Path) -> dict | None:
    """Read-only look at a SQLite file: is it a backtest dataset, and what does it hold?"""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"markets", "candles"} <= tables or not con.execute("SELECT 1 FROM candles LIMIT 1").fetchone():
                return None
            n, t0, t1 = con.execute("SELECT COUNT(*), MIN(close_ts), MAX(close_ts) FROM markets "
                                    "WHERE result IN ('yes','no')").fetchone()
            if n < 50:
                return None
            kv = dict(con.execute("SELECT key, value FROM kv WHERE key IN ('holdout_opened_count','web_validation_looks')")
                      .fetchall()) if "kv" in tables else {}
            return {"path": str(path), "name": path.name, "settled_markets": n, "first_close": t0, "last_close": t1,
                    "series": [r[0] for r in con.execute("SELECT DISTINCT series FROM markets WHERE result IN "
                                                         "('yes','no') ORDER BY series")],
                    "spot_points": con.execute("SELECT COUNT(*) FROM spot").fetchone()[0] if "spot" in tables else 0,
                    "holdout_opened": int(kv.get("holdout_opened_count") or 0),
                    "validation_looks": int(kv.get("web_validation_looks") or 0)}
        finally:
            con.close()
    except sqlite3.Error:
        return None


class Backtests:
    """One backtest at a time, in a background thread, through kbot.backtest.run."""

    def __init__(self, data_dir: str, reports_dir: str):
        self.data_dir, self.reports_dir = data_dir, reports_dir
        self.lock = threading.Lock()
        self.job: dict | None = None
        self._info: dict[str, tuple[float, dict | None]] = {}

    def datasets(self) -> list[dict]:
        out = []
        for p in sorted(Path(self.data_dir).glob("*.sqlite")):
            mtime = p.stat().st_mtime
            if self._info.get(str(p), (None,))[0] != mtime:
                self._info[str(p)] = (mtime, dataset_info(p))
            if (info := self._info[str(p)][1]):
                out.append(info)
        return out

    def history(self) -> list[dict]:
        out = []
        for p in sorted(Path(self.reports_dir).glob("web-backtest-*.json"), reverse=True)[:10]:
            try:
                r = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out.append({"file": p.name, "params": r["params"], "generated_utc": r["generated_utc"],
                        "summary": {k: r["metrics"].get(k) for k in ("trades", "total_net_pnl", "total_fees",
                                                                      "max_drawdown", "ci95_avg_net_per_trade")}})
        return out

    def status(self) -> dict | None:
        with self.lock:
            return dict(self.job) if self.job else None

    def validate(self, p: dict, s: Settings) -> dict:
        ds = {d["path"]: d for d in self.datasets()}
        if p.get("dataset") not in ds:
            raise ValueError("choose one of the listed datasets")
        if p.get("strategy") not in STRATEGIES or p["strategy"] == "maker":
            raise ValueError("choose a strategy that the candle backtest supports (maker needs `kbot maker-history`)")
        if p.get("split") not in SPLITS:
            raise ValueError("the holdout cannot be run from this page; choose train or validation")
        if p["split"] != "train" and p.get("confirm_evaluation") is not True:
            raise ValueError("confirm that you will not tune settings on this evaluation run")
        series = [x for x in p.get("series") or [] if x in ds[p["dataset"]]["series"]]
        try:
            slip, fp, bank = float(p.get("slippage", 0.01)), float(p.get("fill_prob", 0.8)), \
                float(p.get("bankroll", s.risk.bankroll))
        except (TypeError, ValueError):
            raise ValueError("slippage, fill probability and bankroll must be numbers")
        if not (0 <= slip <= 0.10 and 0.05 <= fp <= 1 and 1 <= bank <= 100000):
            raise ValueError("slippage 0 to 0.10, fill probability 0.05 to 1, bankroll $1 to $100,000")
        return {"dataset": p["dataset"], "strategy": p["strategy"], "split": p["split"], "series": series,
                "slippage": slip, "fill_prob": fp, "bankroll": bank}

    def start(self, params: dict, s: Settings) -> dict:
        p = self.validate(params, s)
        with self.lock:
            if self.job and self.job["state"] in ("queued", "running"):
                raise ValueError("a backtest is already running")
            self.job = {"id": secrets.token_hex(4), "state": "running", "phase": "loading data", "progress": 0.0,
                        "started": time.time(), "params": p, "result": None, "error": None}
            job = self.job
        threading.Thread(target=self._work, args=(job, p, s), daemon=True).start()
        return dict(job)

    def _set(self, job, **kw):
        with self.lock:
            job.update(kw)

    def _work(self, job: dict, p: dict, s: Settings) -> None:
        from .backtest import Costs, chronological_split, metrics, run
        from .collector import load_markets, load_spot
        from .feed import UNDERLYING
        try:
            db = DB(p["dataset"])
            ms = load_markets(db, p["series"] or None)
            train, valid, hold = chronological_split(ms)
            part = {"train": train, "validation": valid, "all_but_holdout": train + valid}[p["split"]]
            if p["split"] != "train":       # every look at validation is counted, like the holdout
                db.set("web_validation_looks", (db.get("web_validation_looks") or 0) + 1)
                db.event("validation_viewed", source="web", strategy=p["strategy"])
            spot = load_spot(db)
            self._set(job, phase="replaying markets")
            cfg = replace(s.strategy, name=p["strategy"])
            r = run(part, p["strategy"], cfg, replace(s.risk, bankroll=p["bankroll"]),
                    Costs(slippage=p["slippage"], fill_prob=p["fill_prob"]), spot,
                    {k: v[0] for k, v in UNDERLYING.items()}, progress=lambda f: self._set(job, progress=round(f, 3)))
            m = metrics(r)
            eq, curve = p["bankroll"], []
            for t in sorted(r.trades, key=lambda t: t.close_ts):
                eq += t.pnl
                curve.append((t.close_ts, round(eq, 4)))
            pts = curve[::max(1, len(curve) // 400)]
            if curve and pts[-1] != curve[-1]:
                pts.append(curve[-1])

            def span(xs):
                return [xs[0].info.close_ts, xs[-1].info.close_ts] if xs else None
            result = _clean({
                "label": "HISTORICAL BACKTEST (simulated fills on Kalshi minute candles)",
                "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": p,
                "split": {"train": span(train), "validation": span(valid), "holdout": span(hold),
                          "markets_in_run": len(part), "markets_loaded": len(ms)},
                "metrics": m, "gross": round(m.get("total_net_pnl", 0) + m.get("total_fees", 0), 4) if m["trades"] else 0,
                "baseline_no_trade": 0.0, "equity_curve": pts,
                "validation_looks": db.get("web_validation_looks") or 0,
                "holdout_opened": db.get("holdout_opened_count") or 0, "spot_points": sum(len(v) for v in spot.values()),
                "strategy_config": vars(cfg), "risk": vars(replace(s.risk, bankroll=p["bankroll"]))})
            db.close()
            Path(self.reports_dir).mkdir(parents=True, exist_ok=True)
            Path(self.reports_dir, f"web-backtest-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json").write_text(
                json.dumps(result, indent=2, default=str))
            self._set(job, state="done", phase="done", progress=1.0, result=result, finished=time.time())
        except Exception as e:
            self._set(job, state="error", error=f"{type(e).__name__}: {e}"[:400], finished=time.time())


# --- settings ----------------------------------------------------------------------------------

TOP_FIELDS = {"series": list, "max_data_age_s": float, "max_clock_skew_s": float, "loop_interval_s": float}
RANGES = {"bankroll": (1, 100000), "max_consecutive_losses": (1, 100), "max_open_positions": (1, 100),
          "max_market_inventory": (1, 100), "max_data_age_s": (1, 120), "max_clock_skew_s": (0.5, 30),
          "loop_interval_s": (0.5, 60), "min_seconds_left": (0, 900), "max_seconds_left": (0, 900),
          "max_spread": (0.01, 0.99), "min_net_edge": (0, 0.5), "model_uncertainty": (0, 0.5),
          "min_top_size": (0, 100000), "vol_lookback_s": (60, 7200)}
SERIES_RE = re.compile(r"^KX[A-Z0-9]+15M$")


def editable(s: Settings) -> list[dict]:
    out = [{"section": "", "key": k, "type": t.__name__, "value": getattr(s, k)} for k, t in TOP_FIELDS.items()]
    for sec, obj in (("risk", s.risk), ("strategy", s.strategy)):
        out += [{"section": sec, "key": f.name, "type": type(getattr(obj, f.name)).__name__,
                 "value": getattr(obj, f.name), "range": RANGES.get(f.name)} for f in fields(obj)]
    return out


def api_settings(db: DB, s: Settings, config_path: str) -> dict:
    known = {r["series"] for r in db.q("SELECT DISTINCT series FROM markets WHERE series IS NOT NULL")}
    return {"config": config_path, "exists": Path(config_path).exists(), "mode": s.mode, "data_env": s.data_env,
            "db_path": s.db_path, "production_trading": "ARMED" if s.production_armed() else "disabled",
            "fields": editable(s), "strategies": STRATEGIES, "known_series": sorted(known | set(SERIES))}


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    return json.dumps(v) if isinstance(v, str) else repr(v)


def update_toml(text: str, changes: dict[tuple[str, str], object]) -> str:
    """Set keys in a flat TOML file, keeping its comments and order. Missing keys are appended
    to their section; a missing section is added at the end."""
    out, section, done = [], "", set()

    def flush(sec):
        out.extend(f"{k} = {_toml(v)}" for (sc, k), v in changes.items() if sc == sec and (sc, k) not in done)
        done.update(key for key in changes if key[0] == sec)
    for line in text.splitlines():
        if (h := re.match(r"\s*\[([^\]]+)\]", line)):
            flush(section)
            section = h.group(1).strip()
        elif (m := re.match(r"(\s*)(\w+)(\s*=\s*)(.*?)(\s+#.*)?$", line)) and (section, m.group(2)) in changes:
            line = f"{m.group(1)}{m.group(2)}{m.group(3)}{_toml(changes[(section, m.group(2))])}{m.group(5) or ''}"
            done.add((section, m.group(2)))
        out.append(line)
    flush(section)
    for sec in sorted({sc for sc, k in changes if sc and (sc, k) not in done}):
        out += ["", f"[{sec}]"]
        flush(sec)
    return "\n".join(out) + "\n"


def save_settings(config_path: str, s: Settings, values: dict, confirm_raise: bool = False) -> tuple[Settings, dict]:
    """Validate, write the TOML file (backup kept), and return the reloaded settings and the changes."""
    spec = {(f["section"], f["key"]): f for f in editable(s)}
    changes, errors = {}, []
    for name, raw in values.items():
        sec, _, key = name.rpartition(".")
        f = spec.get((sec, key))
        if not f:
            errors.append(f"{name} cannot be changed here")
            continue
        try:
            if f["type"] == "list":
                v = [x.strip().upper() for x in (raw if isinstance(raw, list) else str(raw).split(",")) if x.strip()]
                if bad := [x for x in v if not SERIES_RE.match(x)]:
                    raise ValueError(f"not a 15-minute series: {', '.join(bad)}")
            elif f["type"] == "str":
                v = str(raw)
                if key == "name" and v not in STRATEGIES:
                    raise ValueError(f"unknown strategy {v}")
            elif f["type"] == "int":
                if isinstance(raw, bool) or float(raw) != int(float(raw)):
                    raise ValueError("must be a whole number")
                v = int(float(raw))
            else:
                v = float(raw)
                if not math.isfinite(v):
                    raise ValueError("must be a number")
            if (r := f.get("range") or RANGES.get(key)) and not r[0] <= v <= r[1]:
                raise ValueError(f"must be between {r[0]:g} and {r[1]:g}")
            if f["type"] in ("int", "float") and v < 0:
                raise ValueError("cannot be negative")
        except (TypeError, ValueError) as e:
            errors.append(f"{name}: {e}")
            continue
        if v != f["value"]:
            changes[(sec, key)] = v
    smin = changes.get(("strategy", "min_seconds_left"), s.strategy.min_seconds_left)
    smax = changes.get(("strategy", "max_seconds_left"), s.strategy.max_seconds_left)
    if smin >= smax:
        errors.append("strategy.min_seconds_left must be below strategy.max_seconds_left")
    if errors:
        raise ValueError("; ".join(errors))
    raised = [f"risk.{k}" for (sec, k), v in changes.items()
              if sec == "risk" and v > getattr(s.risk, k)]
    if raised and not confirm_raise:
        raise PermissionError("this raises risk limits: " + ", ".join(raised))
    if not changes:
        return s, {}
    p = Path(config_path)
    old = p.read_text() if p.exists() else ""
    new = update_toml(old, changes)
    tomllib.loads(new)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(new)
    try:
        s2 = config_mod.load(str(tmp))     # the bot's own loader and validation
    except Exception:
        tmp.unlink()
        raise
    if old:
        p.with_name(p.name + ".bak").write_text(old)
    os.replace(tmp, p)
    return s2, {f"{sec + '.' if sec else ''}{k}": v for (sec, k), v in changes.items()}


# --- HTTP ---------------------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, config_path: str, s: Settings, token: str = "", reports_dir: str = "reports",
                 data_dir: str = "data"):
        self.config_path, self.s, self.token = config_path, s, token
        self.reports_dir = reports_dir
        self.backtests = Backtests(data_dir, reports_dir)
        self.csrf = secrets.token_urlsafe(32)          # local mode: one token per server process
        self.sessions: dict[str, dict] = {}
        self.failures: list[float] = []
        self.proc = None                               # the bot process this server started
        self.lock = threading.Lock()
        super().__init__(addr, Handler)
        host, port = addr[0], self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if host not in LOCAL + ("0.0.0.0", "::", ""):
            self.allowed_hosts.add(f"{host}:{port}")
        extra = {h.strip() for h in os.environ.get("KBOT_WEB_ALLOWED_HOSTS", "").split(",") if h.strip()}
        self.allowed_hosts |= extra
        self.check_host = not token or bool(extra)

    def db(self) -> DB:
        return DB(self.s.db_path)


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "kbot"
    sys_version = ""
    CT = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "text/javascript; charset=utf-8"}
    CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
           "style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; img-src 'self' data:; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def log_message(self, *a):
        pass

    # --- responses
    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                     "X-Frame-Options": "DENY", "Content-Security-Policy": self.CSP, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, headers: dict | None = None):
        self._send(code, json.dumps(_clean(obj), default=str).encode(), "application/json", headers)

    def _err(self, code: int, msg: str):
        self._json(code, {"error": msg})

    # --- auth
    def _session(self) -> dict | None:
        srv = self.server
        if not srv.token:
            return {"csrf": srv.csrf}
        sid = ""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "kbot_session":
                sid = v
        sess = srv.sessions.get(sid)
        if sess and sess["exp"] > time.time():
            return sess
        srv.sessions.pop(sid, None)
        return None

    def _gate(self) -> tuple[str, dict] | None:
        """Common checks. Returns (path, query) or None after sending an error."""
        srv = self.server
        if srv.check_host and self.headers.get("Host", "") not in srv.allowed_hosts:
            self._err(421, "unexpected Host header. Open the page at http://127.0.0.1, or list the host in "
                           "KBOT_WEB_ALLOWED_HOSTS")
            return None
        u = urlsplit(self.path)
        return u.path, parse_qs(u.query)

    def do_GET(self):
        g = self._gate()
        if not g:
            return
        path, q = g
        if path == "/" or path.startswith("/static/"):
            name = "index.html" if path == "/" else path[len("/static/"):]
            f = STATIC / name
            if name not in {"index.html", "app.css", "app.js", "charts.js"} or not f.is_file():
                return self._err(404, "not found")
            return self._send(200, f.read_bytes(), self.CT[f.suffix])
        if path == "/api/session":
            sess = self._session()
            return self._json(200, {"auth_required": bool(self.server.token), "authed": bool(sess),
                                    "csrf": sess["csrf"] if sess else None})
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._session():
            return self._err(401, "log in first")
        srv = self.server
        db = srv.db()
        try:
            one = lambda k, d="": (q.get(k) or [d])[0]
            if path == "/api/state":
                out = api_state(db, srv.s, srv.config_path)
                with srv.lock:
                    if srv.proc is not None:
                        code = srv.proc.poll()
                        out["launch"] = {"pid": srv.proc.pid, "exit_code": code, "log": _tail("logs/web-bot.log")
                                         if code not in (None, 0) else None}
                        if code is None and out["bot"]["state"] == "stopped":
                            out["bot"].update(state="starting", detail="process launched, waiting for its first heartbeat")
                return self._json(200, out)
            if path == "/api/markets":
                return self._json(200, api_markets(db, srv.s))
            if path == "/api/trades":
                return self._json(200, api_trades(db, srv.s, one("q"), one("view", "all"),
                                                  int(one("limit", "100")) if one("limit", "100").isdigit() else 100))
            if path == "/api/decision":
                d = api_decision(db, srv.s, int(one("id", "0"))) if one("id").isdigit() else None
                return self._json(200, d) if d else self._err(404, "no such decision")
            if path == "/api/strategies":
                return self._json(200, api_strategies(db, srv.s, srv.reports_dir))
            if path == "/api/backtest":
                b = srv.backtests
                return self._json(200, {"datasets": b.datasets(), "splits": SPLITS, "job": b.status(),
                                        "history": b.history(), "strategies": [x for x in STRATEGIES if x != "maker"],
                                        "defaults": {"slippage": 0.01, "fill_prob": 0.8, "bankroll": srv.s.risk.bankroll},
                                        "strategy_config": vars(srv.s.strategy), "risk": vars(srv.s.risk)})
            if path == "/api/settings":
                return self._json(200, api_settings(db, srv.s, srv.config_path))
            return self._err(404, "not found")
        finally:
            db.close()

    def do_POST(self):
        g = self._gate()
        if not g:
            return
        path, _ = g
        srv = self.server
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._err(403, "cross-origin request refused")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return self._err(403, "cross-site request refused")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._err(415, "send JSON")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._err(413, "request too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self._err(400, "bad JSON")
        if path == "/api/login":
            return self._login(body)
        sess = self._session()
        if not sess:
            return self._err(401, "log in first")
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), sess["csrf"]):
            return self._err(403, "missing or wrong CSRF token; reload the page")
        if path == "/api/logout":
            srv.sessions = {k: v for k, v in srv.sessions.items() if v is not sess}
            return self._json(200, {"ok": True}, {"Set-Cookie": "kbot_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
        if path == "/api/control":
            return self._control(body)
        if path == "/api/backtest":
            try:
                return self._json(200, srv.backtests.start(body, srv.s))
            except ValueError as e:
                return self._err(400, str(e))
        if path == "/api/settings":
            try:
                with srv.lock:
                    s2, changed = save_settings(srv.config_path, srv.s, body.get("values") or {},
                                                body.get("confirm_raise") is True)
                    srv.s = s2
                db = srv.db()
                try:
                    if changed:
                        db.event("settings_changed", source="web", changes=changed)
                    return self._json(200, {"saved": changed, "settings": api_settings(db, srv.s, srv.config_path)})
                finally:
                    db.close()
            except PermissionError as e:
                return self._json(409, {"error": str(e), "needs_confirm": True})
            except (ValueError, tomllib.TOMLDecodeError) as e:
                return self._err(400, str(e))
        return self._err(404, "not found")

    def _login(self, body: dict):
        srv = self.server
        if not srv.token:
            return self._json(200, {"ok": True})
        now = time.time()
        srv.failures = [t for t in srv.failures if t > now - 600]
        if len(srv.failures) >= 10:
            return self._err(429, "too many failed logins; wait 10 minutes")
        if not hmac.compare_digest(str(body.get("token", "")).encode(), srv.token.encode()):
            srv.failures.append(now)
            time.sleep(0.5)
            return self._err(401, "wrong token")
        sid = secrets.token_urlsafe(32)
        srv.sessions = {k: v for k, v in srv.sessions.items() if v["exp"] > now}
        srv.sessions[sid] = {"exp": now + SESSION_S, "csrf": secrets.token_urlsafe(32)}
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return self._json(200, {"ok": True}, {
            "Set-Cookie": f"kbot_session={sid}; Max-Age={SESSION_S}; Path=/; HttpOnly; SameSite=Strict{secure}"})

    def _control(self, body: dict):
        srv = self.server
        action = body.get("action")
        reason = str(body.get("reason") or "")[:300]
        db = srv.db()
        try:
            with srv.lock:
                st = bot_state(db, srv.s)
                if action == "start":
                    if st["state"] != "stopped" or (srv.proc is not None and srv.proc.poll() is None):
                        raise ControlError("the bot is already running")
                    s_now = config_mod.load(srv.config_path)    # pick up saved settings
                    srv.proc = start_paper(srv.config_path, s_now)
                    db.event("web_start", pid=srv.proc.pid)
                    msg = "Paper bot starting. It shows as running after its first heartbeat."
                elif action == "stop":
                    if body.get("confirm") is not True:
                        raise ControlError("stopping needs confirmation")
                    pid = stop_process(db)
                    msg = f"Stop signal sent to process {pid}. Resting paper bids are cancelled; positions settle."
                elif action == "pause":
                    pause(db, reason or "paused from the web page")
                    msg = "Paused: no new entries. Open positions are still tracked and settled."
                elif action == "resume":
                    resume(db, srv.s, str(body.get("reviewed") or ""), body.get("clear_estop") is True)
                    msg = "Resumed. Limits were not changed."
                elif action == "estop":
                    if body.get("confirm") is not True:
                        raise ControlError("the emergency stop needs confirmation")
                    n = estop(db, srv.s, reason or "emergency stop from the web page")
                    msg = "EMERGENCY STOP set: no new orders." + (f" Cancelled {n} open orders." if n else "") + \
                          " Positions are not closed; they settle normally."
                else:
                    raise ControlError("unknown action")
            return self._json(200, {"ok": True, "message": msg})
        except ControlError as e:
            return self._err(409, str(e))
        finally:
            db.close()


def _tail(path: str, n: int = 20) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def serve(config_path: str, s: Settings, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
          reports_dir: str = "reports", data_dir: str = "data") -> None:
    token = os.environ.get("KBOT_DASHBOARD_TOKEN", "")
    if host not in LOCAL and len(token) < 16:
        raise SystemExit("refusing to serve the control center on a non-local address without "
                         "KBOT_DASHBOARD_TOKEN of 16 or more characters")
    if token and len(token) < 16:
        raise SystemExit("KBOT_DASHBOARD_TOKEN must be 16 or more characters")
    srv = Server((host, port), config_path, s, token, reports_dir, data_dir)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"kbot control center on {url}  (config {config_path}, database {s.db_path})")
    print("Login token required." if token else "Local only, no login. Press Ctrl-C to stop the website "
          "(a bot it started keeps running).")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

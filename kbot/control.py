"""Bot controls shared by the CLI and the web control center: pause, resume, emergency stop,
and starting or stopping the paper-trading process.

The switches are the same persisted flags the trading loop already reads (risk.py, app.py), so
the CLI and the web page always agree. The web page can only ever start paper mode.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .db import DB
from .risk import RiskManager


class ControlError(Exception):
    pass


def pause(db: DB, reason: str) -> None:
    db.set("pause", {"reason": reason, "ts": time.time()})
    db.event("pause", reason=reason)


def resume(db: DB, s: Settings, reviewed: str, clear_estop: bool = False) -> None:
    if db.get("estop") and not clear_estop:
        raise ControlError("emergency stop is engaged; clear it explicitly after you have reviewed why")
    if not reviewed.strip():
        raise ControlError("say what you checked before resuming")
    db.set("pause", None)
    if clear_estop:
        db.set("estop", None)
        db.set("estop_handled", None)
    RiskManager(db, s.risk, s.mode).clear(reviewed)


def estop(db: DB, s: Settings, reason: str) -> int | None:
    """Blocks all new orders at once (persisted). The running bot cancels open entry orders on its
    next tick. In demo/production this also cancels them directly; returns how many, else None."""
    db.set("estop", {"reason": reason, "ts": time.time()})
    db.set("estop_handled", None)
    db.event("estop", reason=reason)
    if s.mode not in ("demo", "production") or not s.api_key_id:
        return None
    from .client import make_trading_client
    from .execution import KalshiAdapter, OrderManager
    from .notify import Notifier

    async def go():
        # cancel_only: this client can cancel but can never create an order.
        om = OrderManager(db, KalshiAdapter(make_trading_client(s, cancel_only=True)), Notifier(db))
        return await om.cancel_open_entries("estop command")
    return asyncio.run(go())


# --- the bot process ------------------------------------------------------------------------------

def _is_kbot(pid: int) -> bool:
    """True if pid is alive and runs kbot. A stale pid from a crash may belong to something else now."""
    if os.name == "nt":     # os.kill(pid, 0) terminates the process on Windows
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return "kbot" in out


def bot_state(db: DB, s: Settings, now: float | None = None) -> dict:
    """running | paused | disconnected | stopped, from the flags and heartbeat the loop writes."""
    now = now or time.time()
    running = db.get("running") or {}
    pid = running.get("pid")
    hb_age = now - (db.get("heartbeat") or 0)
    health = db.get("health") or {}
    halted = RiskManager(db, s.risk, running.get("mode") or s.mode).halted()
    problems = [p for p in (health.get("reason") or "").split("; ") if p not in ("", "ok", "emergency stop engaged")]
    if not pid or not _is_kbot(pid):
        state, detail = "stopped", ("the last run did not shut down cleanly" if pid else "not running")
    elif hb_age > max(30.0, 5 * s.loop_interval_s):
        state, detail = "disconnected", f"no heartbeat for {hb_age:.0f} s"
    elif not health.get("ok") and problems:
        state, detail = "disconnected", "; ".join(problems)
    elif halted:
        state, detail = "paused", halted
    else:
        state, detail = "running", "trading loop healthy"
    return {"state": state, "detail": detail, "pid": pid if state != "stopped" else None,
            "started": running.get("started"), "heartbeat_age_s": round(hb_age, 1), "health": health,
            "estop": db.get("estop"), "pause": db.get("pause"), "halted": halted}


def start_paper(config_path: str | None, s: Settings, log_path: str = "logs/web-bot.log") -> subprocess.Popen:
    """Start `kbot run` in paper mode as its own process, so it outlives the web server.
    Trading keys are left out of its environment; only the optional read-only data key passes."""
    if s.mode != "paper":
        raise ControlError(f"this config is in {s.mode} mode. The web page starts paper mode only; "
                           "anything else goes through the CLI and docs/GO_LIVE.md.")
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("KBOT_DEMO_", "KBOT_PROD_", "KBOT_PRODUCTION")) and not k.startswith("KBOT_PROD_READONLY_"))}
    env["KBOT_MODE"] = "paper"
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "kbot", *(["--config", config_path] if config_path else []), "run"]
    with open(log_path, "ab") as log:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)


def stop_process(db: DB) -> int:
    """SIGTERM the running bot. It cancels resting paper bids and leaves positions to settle."""
    pid = (db.get("running") or {}).get("pid")
    if not pid or not _is_kbot(pid):
        raise ControlError("no running bot process found on this computer")
    os.kill(pid, signal.SIGTERM)
    db.event("stop_requested", pid=pid)
    return pid

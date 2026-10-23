"""Command line entry point: `python -m kbot <command>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import replace

from . import config


def _setup_logging(level: str) -> None:
    class Json(logging.Formatter):
        def format(self, r):
            d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)) + "Z",
                 "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)
            return json.dumps(d)
    h = logging.StreamHandler()
    h.setFormatter(Json())
    logging.basicConfig(level=level, handlers=[h])


def _db(s):
    from .db import DB
    return DB(s.db_path)


def cmd_discover(s, a):
    from .client import make_public_client
    from .discovery import Discovery

    async def go():
        c = make_public_client(s)
        d = Discovery(c, _db(s), s.series or None)
        ms = await d.refresh()
        for name, meta in d.series_meta.items():
            print(f"{name:20s} fee_type={meta.get('fee_type')} mult={meta.get('fee_multiplier')} "
                  f"sources={[x.get('name') for x in meta.get('settlement_sources') or []]}")
        for m in sorted(ms.values(), key=lambda m: m.close_ts):
            print(f"  {m.ticker:32s} window={m.window_s / 60:.0f}m left={m.seconds_left() / 60:5.1f}m "
                  f"strike={m.floor_strike} status={m.status}")
        await c.close()
    asyncio.run(go())


def cmd_collect(s, a):
    from .client import make_public_client
    from .collector import collect_history
    from .discovery import Discovery

    async def go():
        # Unauthenticated reads were throttled near 3.5 req/s (2026-10-23), far below the documented
        # budget, so pace at 3 req/s instead of burning time in 429 backoff.
        c = make_public_client(s, max_retries=8, read_tokens_per_s=30)
        db = _db(s)
        series = s.series or await Discovery(c, db).find_series()
        print(await collect_history(c, db, series, max_markets=a.max_markets,
                                    since_ts=time.time() - a.days * 86400))
        await c.close()
    asyncio.run(go())


def cmd_collect_spot(s, a):
    from .collector import collect_coinbase_spot
    from .feed import UNDERLYING
    db = _db(s)
    for series in (s.series or list(UNDERLYING)):
        idx, product = UNDERLYING[series]
        n = asyncio.run(collect_coinbase_spot(db, product, idx, time.time() - a.days * 86400, time.time()))
        print(f"{series}: {n} one-minute Coinbase points stored as proxy for {idx}")


def cmd_import_spot(s, a):
    from .collector import import_spot_csv
    print(import_spot_csv(_db(s), a.csv, a.index), "rows")


def cmd_verify_data(s, a):
    from pathlib import Path
    from .collector import verify_history
    rep = verify_history(_db(s), s.series or None)
    Path("reports").mkdir(exist_ok=True)
    Path("reports/data-verification.json").write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))


def cmd_research(s, a):
    from .research import research
    rep = research(_db(s), s, open_holdout=a.open_holdout)
    print(json.dumps({k: rep[k] for k in ("label", "markets_total", "verdict") if k in rep}, indent=2))
    print("full report: reports/research-latest.json")


def cmd_audit(s, a):
    from .audit import audit
    rep = audit(_db(s), s, open_holdout=a.open_holdout)
    print(json.dumps({k: rep[k] for k in ("label", "strategy", "markets", "split") if k in rep}, indent=2))
    print("full report: reports/edge-audit.json")


def cmd_record(s, a):
    from .db import DB
    from .recorder import Recorder
    asyncio.run(Recorder(s, DB(a.db), book_every_s=a.book_every).run())


def cmd_backfill(s, a):
    from .client import make_public_client
    from .db import DB
    from .experiments import PROSPECTIVE_START
    from .recorder import backfill

    async def go():
        c = make_public_client(s, max_retries=8, read_tokens_per_s=30)
        print(await backfill(c, DB(a.db), PROSPECTIVE_START), "markets got trades")
        await c.close()
    asyncio.run(go())


def cmd_experiment(s, a):
    from .db import DB
    from .experiments import evaluate, status
    db = DB(a.db)
    if a.action == "status":
        print(json.dumps(status(db), indent=2, default=str))
        return
    if not a.name:
        sys.exit("name the experiment: kbot experiment evaluate E1 --final")
    rep = evaluate(db, a.name, final=a.final, force=a.force, interim=a.interim)
    print(json.dumps({k: rep[k] for k in ("label", "experiment", "spec_hash", "final_openings", "warning",
                                          "interim") if k in rep}, indent=2))
    print(f"full report: reports/experiment-{a.name}{'-interim' if a.interim else ''}.json")


def cmd_maker_history(s, a):
    from pathlib import Path
    from .client import make_public_client
    from .db import DB
    from .maker import history
    if not Path(a.db).exists():
        sys.exit(f"{a.db} not found. It needs settled markets and minute candles: copy the 90-day dataset "
                 "there, or run `kbot collect --days 90` with db_path pointing at it.")

    async def go():
        c = None if a.no_download else make_public_client(s, max_retries=8, read_tokens_per_s=20)
        rep = await history(c, DB(a.db))
        if c:
            await c.close()
        return rep
    rep = asyncio.run(go())
    Path("reports").mkdir(exist_ok=True)
    Path("reports/maker-history.json").write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps({k: rep.get(k) for k in ("label", "sample", "markets", "missing")}, indent=2))
    print(json.dumps(rep.get("primary", {}).get("net_zero_fee"), indent=2))
    print("full report: reports/maker-history.json")


def cmd_backtest(s, a):
    from .backtest import Costs, chronological_split, metrics, run
    from .collector import load_markets, load_spot
    from .feed import UNDERLYING
    db = _db(s)
    ms = load_markets(db, s.series or None)
    train, valid, hold = chronological_split(ms)
    part = {"train": train, "validation": valid, "all_but_holdout": train + valid}[a.split]
    cfg = replace(s.strategy, name=a.strategy)
    r = run(part, a.strategy, cfg, s.risk, Costs(slippage=a.slippage, fill_prob=a.fill_prob),
            load_spot(db), {k: v[0] for k, v in UNDERLYING.items()})
    print(json.dumps({"label": "HISTORICAL BACKTEST", "split": a.split, **metrics(r)}, indent=2, default=str))


def cmd_run(s, a):
    from .app import App
    if s.mode == "production" and not s.production_armed():
        sys.exit("production mode is not armed. See docs/LIVE_CHECKLIST.md. Refusing to start.")
    asyncio.run(App(s).run())


def cmd_status(s, a):
    from .dashboard import state
    st = state(_db(s), s)
    for k in ("mode_label", "running", "heartbeat_age_s", "health", "halted", "risk_utilization",
              "last_reconcile", "reconcile_problems"):
        print(f"{k}: {st[k]}")
    p = st["performance"]
    print({k: p[k] for k in ("label", "settled_trades", "net_pnl", "win_rate", "max_drawdown", "fees")})
    print(f"open positions: {st['positions']}\noutstanding orders: {st['orders']}")
    if s.strategy.name == "maker":
        from .maker import format_report, report
        print("\n" + format_report(report(_db(s), st["mode"], s.risk.bankroll)))


def cmd_pause(s, a):
    db = _db(s)
    db.set("pause", {"reason": a.reason, "ts": time.time()})
    db.event("pause", reason=a.reason)
    print("paused: no new entries. Open positions are still tracked and settled.")


def cmd_resume(s, a):
    from .risk import RiskManager
    db = _db(s)
    if db.get("estop") and not a.clear_estop:
        sys.exit("emergency stop is engaged; pass --clear-estop after you have reviewed why")
    if not a.reviewed:
        sys.exit("pass --reviewed 'what you checked' to resume")
    db.set("pause", None)
    if a.clear_estop:
        db.set("estop", None)
        db.set("estop_handled", None)
    RiskManager(db, s.risk, s.mode).clear(a.reviewed)
    print(f"resumed {s.mode}. Limits were not changed.")


def cmd_estop(s, a):
    """Blocks all new orders at once (persisted). The running bot cancels open entry orders on its
    next tick. In demo/production this command also cancels them directly."""
    db = _db(s)
    db.set("estop", {"reason": a.reason, "ts": time.time()})
    db.set("estop_handled", None)
    db.event("estop", reason=a.reason)
    print("EMERGENCY STOP set: no new orders.")
    if s.mode in ("demo", "production") and s.api_key_id:
        from .client import make_trading_client
        from .execution import KalshiAdapter, OrderManager
        from .notify import Notifier

        async def go():
            # cancel_only: this client can cancel but can never create an order.
            om = OrderManager(db, KalshiAdapter(make_trading_client(s, cancel_only=True)), Notifier(db))
            print("cancelled", await om.cancel_open_entries("estop command"), "open entry orders")
        asyncio.run(go())
    print("Existing positions are NOT closed. They settle normally. Check them with: python -m kbot status")


def cmd_reconcile(s, a):
    from .app import App

    async def go():
        app = App(s)
        await app.reconcile()
        print("problems:", app.db.get(f"reconcile_problems:{s.mode}"))
    asyncio.run(go())


def cmd_check_auth(s, a):
    from .client import AccountClient, make_signer
    from .config import ENVIRONMENTS

    async def go():
        env = "production" if s.mode == "production" else "demo"
        c = AccountClient(ENVIRONMENTS[env]["rest"], make_signer(s))
        b = await c.balance()
        print(f"{env} credentials OK. balance_dollars={b.get('balance_dollars')}")
        await c.close()
    asyncio.run(go())


def cmd_report(s, a):
    from .reports import daily_report
    print(daily_report(_db(s), s.mode, s.risk.bankroll))


def cmd_dashboard(s, a):
    from .dashboard import serve
    serve(s.db_path, s, a.host or s.dashboard_host, a.port or s.dashboard_port)


def cmd_backup(s, a):
    print(_db(s).backup())


def main(argv=None):
    p = argparse.ArgumentParser(prog="kbot")
    p.add_argument("--config", default=None)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="list live ~15 minute series and open markets")
    c = sub.add_parser("collect", help="download settled markets + minute candles")
    c.add_argument("--max-markets", type=int, default=3000)
    c.add_argument("--days", type=float, default=60)
    c = sub.add_parser("collect-spot", help="download Coinbase 1-minute prices as an index proxy")
    c.add_argument("--days", type=float, default=60)
    c = sub.add_parser("import-spot", help="import a CSV of ts,price for an index")
    c.add_argument("csv")
    c.add_argument("--index", required=True)
    sub.add_parser("verify-data", help="audit stored history: timing, settlement, gaps, quotes")
    c = sub.add_parser("research", help="strategy comparison report")
    c.add_argument("--open-holdout", action="store_true")
    c = sub.add_parser("audit", help="edge audit: backtest checks, failure mode, baselines, calibration")
    c.add_argument("--open-holdout", action="store_true")
    c = sub.add_parser("record", help="record books, CF Benchmarks and Coinbase ticks for the prospective tests")
    c.add_argument("--db", default="data/prospective.sqlite")
    c.add_argument("--book-every", type=float, default=2.0)
    c = sub.add_parser("backfill", help="download settled markets, candles and trades since 2026-10-24")
    c.add_argument("--db", default="data/prospective.sqlite")
    c = sub.add_parser("experiment", help="prospective experiments E1 and E2: status or evaluate")
    c.add_argument("action", choices=["status", "evaluate"])
    c.add_argument("name", nargs="?", choices=["E1", "E2"])
    c.add_argument("--db", default="data/prospective.sqlite")
    c.add_argument("--final", action="store_true", help="the one valid reading, after the end date")
    c.add_argument("--interim", action="store_true", help="E2's single futility look after day 28")
    c.add_argument("--force", action="store_true", help="read early: counted and labelled not valid")
    c = sub.add_parser("maker-history", help="maker rule on downloadable history (candles + public trades)")
    c.add_argument("--db", default="data/maker-history.sqlite", help="a copy with markets and candles; trades are added")
    c.add_argument("--no-download", action="store_true")
    c = sub.add_parser("backtest")
    c.add_argument("--strategy", default="fair_value")
    c.add_argument("--split", default="validation", choices=["train", "validation", "all_but_holdout"])
    c.add_argument("--slippage", type=float, default=0.01)
    c.add_argument("--fill-prob", type=float, default=0.8)
    sub.add_parser("run", help="run the bot in the configured mode (paper by default)")
    sub.add_parser("status")
    c = sub.add_parser("pause")
    c.add_argument("--reason", default="manual pause")
    c = sub.add_parser("resume")
    c.add_argument("--reviewed", default="")
    c.add_argument("--clear-estop", action="store_true")
    c = sub.add_parser("estop")
    c.add_argument("--reason", default="manual emergency stop")
    sub.add_parser("reconcile")
    sub.add_parser("check-auth")
    sub.add_parser("report")
    c = sub.add_parser("dashboard")
    c.add_argument("--host")
    c.add_argument("--port", type=int)
    sub.add_parser("backup")
    a = p.parse_args(argv)
    _setup_logging(a.log_level)
    s = config.load(a.config)
    import httpx
    try:
        globals()["cmd_" + a.cmd.replace("-", "_")](s, a)
    except httpx.HTTPError as e:
        sys.exit(f"network error: {e!r}. Check connectivity to Kalshi and try again.")


if __name__ == "__main__":
    main()

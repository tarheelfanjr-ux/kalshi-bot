# Architecture

```
            +------------------+      REST (public)       +------------------+
            |  discovery.py    |<------------------------>|                  |
            |  15M series,     |                          |   Kalshi API     |
            |  eligible mkts   |      WS (auth) / REST    |  demo or prod    |
            +--------+---------+<------------------------>|                  |
                     |            feed.py: books, index   +---------^--------+
                     v                                              |
  +-----------+  Snapshot  +-------------+ Candidate +---------+ approve +---------------+
  |  model.py |----------->| strategy.py |---------->| risk.py |-------->| execution.py  |
  +-----------+            +-------------+           +---------+         | OrderManager  |
                                                          ^              | + Adapter:    |
                                                          |              | Paper / Kalshi|
                                                     +----+----+         +-------+-------+
                                                     |  db.py  |<----------------+
                                                     | SQLite  |  orders, fills, positions,
                                                     +----+----+  ledger, signals, events
                                                          |
                       web.py + web/ (browser), cli.py -> control.py, dashboard.py, reports.py
```

| Module | Responsibility |
|---|---|
| `config.py` | Settings from TOML + env. Secrets only from env. Production arming rule. |
| `auth.py` | Ed25519 / RSA-PSS request signing. Refuses world-readable key files. |
| `client.py` | `PublicClient`, `AccountClient`, `TradingClient`. Rate limiter, retries for reads only, `OrderStateUnknown` on create timeouts, no fund-transfer endpoints. |
| `discovery.py` | Finds series ending in `15M`, keeps markets whose window is 10 to 20 minutes, eligibility checks. |
| `book.py` | Bid-only book, derived asks, depth walking for executable prices. |
| `feed.py` | REST book polling, authenticated WebSocket (books, index, lifecycle) with reconnect and seq-gap resync, Coinbase proxy spot. |
| `model.py` | Settlement probability with 60 s averaging, realized volatility. |
| `strategy.py` | `Candidate` with every required field, strategies, benchmarks. |
| `risk.py` | Central gate: halts, health, worst-case sizing, aggregate risk incl. resting orders, daily loss ledger, loss streak, max positions, no averaging. |
| `execution.py` | Order state machine, durable intent before send, reconciliation, settlement accounting, Paper and Kalshi adapters. |
| `backtest.py` | Event-driven replay on minute candles through the same risk and execution code. Metrics, bootstrap CIs, splits. |
| `collector.py` | Historical markets and candles, Coinbase proxy prices, CSV import, public trades summed per second. |
| `research.py` | Calibration, strategy comparison, random baseline, sensitivity, holdout control, verdict. |
| `recorder.py` | Read-only prospective recorder: books, CF RTI and Coinbase ticks, fee state, periodic backfill. |
| `maker.py` | Maker strategy: E2's quotes live in paper mode, fills from the public trade tape, comparison report, historical check. |
| `experiments.py` | Frozen specs and once-only scoring for prospective experiments E1 and E2. |
| `app.py` | Long-running loop, health, settlement, reconciliation, snapshots for future research. |
| `reports.py` | Per-mode performance, calibration, deterioration check, daily report. |
| `dashboard.py` | Bot state for `kbot status` and the web control center. No secrets. |
| `control.py` | Pause, resume, emergency stop, bot state, start (paper only) and stop of the bot process. Shared by the CLI and the web. |
| `web.py`, `web/` | Web control center: stdlib HTTP server, JSON API, login and CSRF, settings editor, backtest runner, single-page UI. See [WEB.md](WEB.md). |
| `notify.py` | Log + optional webhook. |

## Order state machine

```
pending_submit --> submitted --> resting --> partially_filled --> filled
      |               |            |               |
      |               +--> unknown <+---------------+   (timeout / 5xx: reconcile, never resend)
      +--> rejected, canceled, filled (IOC results)
unknown --> any reported state, or rejected if not found on the exchange after 60 s
```

Terminal: `filled`, `canceled`, `rejected`. Illegal transitions raise.

## Database tables

`orders`, `fills`, `positions`, `ledger` (daily realized P&L), `signals` (every accept and
reject with reason), `events` (audit log), `markets`, `candles`, `snapshots` (live book and
spot every 10 s for future research), `spot`, `equity`, `kv` (halt, pause, estop, health).
Every money row has a `mode` column.

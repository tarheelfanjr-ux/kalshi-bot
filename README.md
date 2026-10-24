# kbot: Kalshi 15-minute market research and trading bot

Research, backtest, paper trade, and (only after manual approval) trade Kalshi markets
that open and close in about 15 minutes, such as `KXBTC15M` (bitcoin up or down).

**Production trading is disabled by default.** No strategy has been shown to be
profitable. A 90-day real-data backtest (2026-10-23) found no edge. See [docs/REPORT.md](docs/REPORT.md) for what is and is not verified.

## Quick start

```bash
scripts/setup.sh                         # venv, dependencies, config files, 114 tests (no network needed)
.venv/bin/python -m kbot discover        # list live 15-minute series and markets
.venv/bin/python -m kbot collect --days 60          # settled markets + minute candles
.venv/bin/python -m kbot verify-data                # audit settlement, timing, gaps
.venv/bin/python -m kbot collect-spot --days 60     # Coinbase 1-min prices (index proxy)
.venv/bin/python -m kbot research        # strategy comparison -> reports/research-latest.json
.venv/bin/python -m kbot audit           # why the strategy loses -> reports/edge-audit.json
.venv/bin/python -m kbot record          # prospective recorder for experiments E1 and E2 (read-only)
.venv/bin/python -m kbot experiment status   # E1/E2 coverage and dates, no outcomes
.venv/bin/python -m kbot run             # continuous paper trading (default mode)
.venv/bin/python -m kbot --config config.maker.toml run      # maker strategy, paper only
.venv/bin/python -m kbot --config config.maker.toml status   # its comparison report
.venv/bin/python -m kbot dashboard       # http://127.0.0.1:8765
```

## Documents

| File | What it covers |
|---|---|
| [docs/RESEARCH.md](docs/RESEARCH.md) | Verified Kalshi API, fee, and market facts with sources |
| [docs/PLAN.md](docs/PLAN.md) | Strategy design, test method, acceptance criteria |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules and data flow |
| [docs/OPERATING_MANUAL.md](docs/OPERATING_MANUAL.md) | Setup, credentials, every command, recovery |
| [docs/LIVE_CHECKLIST.md](docs/LIVE_CHECKLIST.md) | Steps required before any real-money order |
| [docs/GO_LIVE.md](docs/GO_LIVE.md) | Maker strategy: paper, then demo, then production, and who lifts each guard |
| [docs/REPORT.md](docs/REPORT.md) | Status, results, audit, limitations |
| [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) | Prospective experiments E1 and E2, and P1, the frozen 14-day paper test |
| [docs/HOSTING.md](docs/HOSTING.md) | Keeping the paper bot and recorder running 14 days: Mac or a $6 server, restarts |

## Result categories

Every number the bot reports carries one of these labels and they are never combined:

1. HISTORICAL BACKTEST (simulated fills on Kalshi minute candles)
2. SIMULATED PAPER TRADING (live data, simulated fills)
3. KALSHI DEMO ACCOUNT (exchange-simulated, not real money)
4. REAL MONEY PRODUCTION

Reports from `kbot experiment` carry their own label, PROSPECTIVE TEST (simulated fills on data
recorded after 2026-10-23).

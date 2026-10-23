# Operating manual

All commands run from the project folder. `kbot` below means `.venv/bin/python -m kbot`.

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
cp .env.example .env          # edit, then: set -a; source .env; set +a
.venv/bin/pytest              # all tests must pass before anything else
```

Network access needed: `external-api.kalshi.com`, `external-api-ws.kalshi.com`,
`external-api.demo.kalshi.co`, `external-api-ws.demo.kalshi.co`, and for the spot proxy
`api.exchange.coinbase.com`.

## 2. Kalshi demo credentials (do this on your own machine)

1. Create a demo account through the link on https://docs.kalshi.com/getting_started/demo_env (separate from your real account).
2. In demo account settings, create an API key. Kalshi shows the private key once. Save it
   straight to a file, never paste it into chat, code, or config:
   ```bash
   mkdir -p ~/.kalshi && chmod 700 ~/.kalshi
   # save the downloaded key as ~/.kalshi/demo_private_key.pem
   chmod 600 ~/.kalshi/demo_private_key.pem
   ```
3. In `.env` set `KBOT_DEMO_KEY_ID=<key id>` and
   `KBOT_DEMO_PRIVATE_KEY_PATH=~/.kalshi/demo_private_key.pem`.
4. Check: `KBOT_MODE=demo kbot check-auth` prints the demo balance.

The bot refuses key files that other users can read. It never asks for or stores your
Kalshi password. Demo and production keys use different variables (`KBOT_DEMO_*`,
`KBOT_PROD_*`) and the bot picks them by mode, so one can never stand in for the other.

Optional: for paper mode on production data with the WebSocket and the CF Benchmarks index
feed, create a production key with read-only permission if Kalshi offers that scope, and set
`KBOT_PROD_READONLY_KEY_ID` / `KBOT_PROD_READONLY_PRIVATE_KEY_PATH`. It is used only for
market data. Without it, paper mode polls books over REST and uses Coinbase as the spot proxy.

## 3. Research workflow

```bash
kbot discover                    # confirm which 15M series exist right now, fee type, sources
kbot collect --days 60           # settled markets + 1-minute candles (resumable, ~1 hour per 90 days x 4 series)
kbot collect --days 60 --trades  # the same, plus every public trade in those markets
kbot verify-data                 # settlement rule, ticker times, gaps, quotes -> reports/data-verification.json
kbot collect-spot --days 60      # Coinbase 1-minute prices, stored as proxy for each RTI index
kbot import-spot file.csv --index BRTI   # optional: real index data you are licensed to use
kbot collect-pyth --days 70      # Pyth 1-minute bars for the 13 Pyth-settled candidate series. Needs
                                 # PYTH_API_KEY in .env. Prints how many open strikes match each offset
kbot research                    # writes reports/research-latest.json, holdout stays closed
kbot backtest --strategy fair_value --split validation --slippage 0.02
kbot research --open-holdout     # ONLY once a strategy passes validation; each opening is counted
```

Read `verdict` in the report. "NO STRATEGY PASSED" means do not trade.

### Prospective experiments

E1 and E2 ([EXPERIMENTS.md](EXPERIMENTS.md)) use only data recorded after 2026-10-23, in
`data/prospective.sqlite`. The recorder is read-only. It never builds a trading client.

```bash
kbot record                      # keep running: books every 2 s, Coinbase ticks, RTI ticks if keyed,
                                 # plus settled markets, candles and trades every 15 minutes
kbot backfill                    # fills download gaps (markets, candles, trades) since 2026-10-24
kbot experiment status           # coverage, dates, recorder heartbeat, fee state. No outcomes
kbot experiment evaluate E2 --interim   # once, after 2026-11-22. Futility only
kbot experiment evaluate E2 --final     # once, after the end date -> reports/experiment-E2.json
kbot experiment evaluate E1 --final     # once, after D0 + 56 days
```

E1 needs the read-only production key above, set on the machine that runs `kbot record`. Without
it E1 shows "not started" and everything else still records. Run the recorder on a machine that
stays on (sleep off), or as a service like the one in section 4 with `ExecStart=... -m kbot record`.
A one-hour test with the four crypto series wrote about 3 MB. The recorder now also records 13
candidate series (copper, platinum, palladium, five FX pairs, Brent and four Treasury yields), so
plan on roughly 100 to 200 MB a day. That figure is an estimate: check the file size after a day.
E2's primary test needs only `kbot backfill`, which can run any time later.

## 4. Continuous paper trading

```bash
KBOT_MODE=paper kbot run         # Ctrl-C or SIGTERM shuts down cleanly
kbot dashboard                   # separate terminal, http://127.0.0.1:8765
kbot status                      # text summary
kbot report                      # daily report
```

State lives in `data/kbot.sqlite`. Restarting resumes from it. Paper results are labelled
SIMULATED and stored with `mode='paper'`.

With the default $25 bankroll and 1% per-position risk, the most a single position may lose is
$0.25, so the bot can only buy one contract priced at or under about 23 cents. Most signals will be
skipped for size. To gather statistics faster in paper mode, set `[risk] bankroll = 500` in
`config.toml`. That changes only the simulation.

Run it as a service (Linux, systemd), restarting on crash:

```ini
# ~/.config/systemd/user/kbot-paper.service
[Unit]
Description=kbot paper trading
After=network-online.target
[Service]
WorkingDirectory=%h/kalshi-bot
EnvironmentFile=%h/kalshi-bot/.env
ExecStart=%h/kalshi-bot/.venv/bin/python -m kbot run
Restart=on-failure
RestartSec=30
[Install]
WantedBy=default.target
```

`systemctl --user enable --now kbot-paper`. On macOS use a launchd plist with the same command.

### Maker strategy (paper only)

The maker strategy runs E2's rule ([EXPERIMENTS.md](EXPERIMENTS.md)) live on production data, with
no key. It cannot place real orders: it refuses to start in any mode but paper. The real-order
path exists and is tested against a mocked exchange. Lifting the guard is the owner's decision,
step by step in [GO_LIVE.md](GO_LIVE.md).

```bash
python -m kbot --config config.maker.toml run          # start. Ctrl-C stops it cleanly
python -m kbot --config config.maker.toml status       # health, limits, and the comparison report
python -m kbot --config config.maker.toml dashboard    # http://127.0.0.1:8765
python -m kbot --config config.maker.toml pause --reason "..."   # no new quotes
python -m kbot --config config.maker.toml estop --reason "..."   # block everything
```

What it does each minute from 840 s to 60 s before close, in BTC, ETH, SOL and XRP: post a bid at
the best YES bid and at the best NO bid. A bid fills only when a public trade prints at a worse
price on its side at least 1 s after it was posted. A trade at exactly our price does not fill it.
At most one fill per side per market. Held to settlement. Fees use the series' fee type (no maker
fee on these series today), with 0.0175 and the taker rate shown as stress cases.

Two records come out of the same quotes and the same fills:

* **Research log** (`quotes` table): every quote the rule wanted, 1 contract each, no risk limits.
  This is the short prospective test of the rule.
* **Paper account** (`orders`, `fills`, `positions`): only what the `[risk]` limits allow. With the
  $25 bankroll and 1% per position, only bids at about 0.25 or less fit, so the account holds the
  cheap side of a market and never the hedge.

fair_value runs beside it as a shadow benchmark (first signal per market, 1 contract at the ask,
taker fee). It is logged, never traded. `status` shows all three next to the no-trade baseline,
gross and net, with 95% CIs that resample whole settlement windows.

The short test is a check that the bot works and a rough read. It is not E2's frozen evaluation.
Changing the rule because of what it shows makes E2 invalid. Read `status`, not single trades.

`kbot maker-history --db <copy with markets and candles>` runs the same rule on a seeded sample of
markets that closed before the audit holdout (Oct 5, 14:15 UTC), downloading their public trades.

## 5. Demo account trading

`KBOT_MODE=demo kbot run`. Orders go to the demo exchange, priced off demo books. On start it
reconciles orders and positions with the exchange before any new order, and every 30 seconds after.

## 6. Pause, resume, emergency stop

| Action | Command | Effect |
|---|---|---|
| Pause | `kbot pause --reason "..."` | No new entries. Positions keep being tracked and settled. |
| Emergency stop | `kbot estop --reason "..."` | No new orders. Open entry orders are cancelled (directly in demo/production, and by the running bot on its next tick). |
| Resume | `kbot resume --reviewed "what I checked"` | Clears pause and risk halts. Add `--clear-estop` after an e-stop. Limits are never changed. |

Stopping the program does **not** close positions. They settle at expiry on the exchange.

## 7. When the bot halts itself

It stops opening positions (and cancels open entry orders) when: the daily realized loss hits
2% of bankroll (persisted, resets at 00:00 UTC), 5 losses in a row, a reconciliation mismatch,
insufficient funds, or an e-stop. It also refuses entries while unhealthy: stale books (>10 s),
no successful API call in 60 s, clock skew >2 s, WebSocket down, or reconciliation older
than 120 s. Check `kbot status` and the dashboard's errors panel, fix the cause, then resume.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `network error: ProxyError 403` | Your network blocks Kalshi hosts. |
| `401` on `check-auth` | Key id and key file mismatch, wrong environment, or a revoked key. |
| `clock skew` | Enable NTP: `timedatectl set-ntp true` (Linux) or Date & Time settings. |
| `reconciliation mismatch` | Compare `kbot status` with the Kalshi website. Never "fix" by deleting rows. |
| `database integrity check failed` | Stop the bot. Copy the newest file from `data/backups/` over `data/kbot.sqlite`. |

Logs are JSON lines on stderr. Redirect them to a file if running in the background.

## 9. Backups

The running bot writes a daily backup to `data/backups/` (14 kept). Manual: `kbot backup`.

## 10. Credential rotation (if a key may be exposed)

1. `kbot estop --reason "key rotation"`.
2. In Kalshi account settings, delete the exposed API key immediately.
3. Create a new key, save it as a new file with `chmod 600`, update `.env`.
4. Delete the old key file (`shred -u` on Linux).
5. `kbot check-auth`, then `kbot resume --reviewed "rotated key" --clear-estop`.
6. Review fills on the Kalshi website for anything you did not place.

Never commit `.env`, `config.toml`, key files, `data/`, or `reports/` (all in `.gitignore`).

# Go-live runbook: maker strategy

Status on 2026-10-23: **NO-GO for real money.** The bot works in paper mode. The real-order path is
built and tested against a mocked exchange. The strategy has no edge so far: on 1,298 markets that
closed before the audit holdout, the maker rule lost 1.82 cents per contract before fees (95% CI
-2.06 to -1.59). It earns when both sides fill on the first quote and loses more when only one side
fills before the price runs. The go/no-go report has the details and what would change the verdict:
[Kalshi bot go/no-go](https://claude.ai/artifact/DB1Pm7VzzJCgucDz6b7xcY).

Three stages. Each one has its own guard, and only you can lift it.

| Stage | Money | What it proves | Guard |
|---|---|---|---|
| 1. Paper | none | The bot runs for weeks. The rule's fills and P&L, gross and net. | none |
| 2. Demo | fake | Real orders, cancels, e-stop and reconciliation on Kalshi's demo exchange. | maker refuses demo until you edit one line |
| 3. Production | real | Whether the fill model holds with real queue position. | the line above, plus the arming variables in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md) |

## Stage 0: set up (once)

```bash
git clone https://github.com/tarheelfanjr-ux/kalshi-bot && cd kalshi-bot
scripts/setup.sh            # venv, dependencies, local config files, all tests
```

macOS or Linux. On Windows use WSL. The machine must stay awake. Sync its clock (NTP).

## Stage 1: paper (start now)

```bash
nohup .venv/bin/python -m kbot --config config.maker.toml run >> logs/paper-maker.log 2>&1 &
.venv/bin/python -m kbot --config config.maker.toml status       # any time
.venv/bin/python -m kbot --config config.maker.toml dashboard    # http://127.0.0.1:8765
```

No key needed. It reads production market data and fills simulated bids from the public trade tape.
State lives in `data/paper-maker.sqlite`. A restart picks up where it stopped.

Read these lines in `status`:

* **Maker rule, every quote at 1 contract.** The research log. Look at `net per contract` and its
  95% CI. One good window means nothing.
* **net if a 0.0175 maker fee applied.** If this goes negative, the edge is too thin to survive a fee
  change.
* **Paper account.** What the $25 limits allow. At 1% per position only bids at 0.25 or less fit, so
  the account buys the cheap side alone and never holds the hedge. Its results differ from the log.
* **fair_value shadow and no-trade baseline.** The strategy must beat no-trade after fees.

The bot halts itself on the daily loss limit (2%), 5 losses in a row, or stale data. That is the
design. Check the reason in `status`, then `kbot ... resume --reviewed "what I checked"`.

Exit criteria for stage 1, set on 2026-10-23 before the data came in (do not move them after a look):
14 days with no gap over 2 hours, 300+ settled research-log fills across 100+ windows, research-log
net per contract with a 95% CI above zero, its mean still above zero with a 0.0175 maker fee, and
50+ settled paper-account trades.

## Stage 2: demo (fake money, needs your decision)

The maker strategy refuses demo mode today. To allow demo **only**, change this check in
`kbot/app.py`:

```python
if maker and s.mode != "paper":
```

to:

```python
if maker and s.mode == "production":
```

and update `test_maker_refuses_any_mode_but_paper` in `tests/test_maker.py` to expect a refusal in
production mode only. Commit that change yourself so the history shows who made it.

Then:

1. Create the demo key on your machine: [OPERATING_MANUAL.md section 2](OPERATING_MANUAL.md#2-kalshi-demo-credentials-do-this-on-your-own-machine).
   Never paste a key into chat.
2. Make `config.maker-demo.toml`: a copy of `config.maker.toml` with
   `mode = "demo"`, `data_env = "demo"` and `db_path = "data/demo-maker.sqlite"`.
3. `set -a; source .env; set +a` and `KBOT_MODE=demo .venv/bin/python -m kbot check-auth`.
4. Run it beside the paper bot:
   `nohup .venv/bin/python -m kbot --config config.maker-demo.toml run >> logs/demo-maker.log 2>&1 &`
5. Rehearse while a bid is resting: `kbot --config config.maker-demo.toml estop --reason drill`.
   Check on the demo website that the order is gone. Then `resume --reviewed "drill" --clear-estop`.
6. Stop the bot with Ctrl-C or `kill <pid>` (SIGTERM) while a bid rests. It must cancel the bid on
   the way out. Check the website.

What demo does on each quote: a post-only, good-till-cancelled bid that expires at market close. It
is cancelled the moment its quote ends. Before a new bid on the same side, the old one is cancelled
and read back, so a fill that beat the cancel is counted. Fills come only from the exchange.
`status` adds a **fill model check**: closed quotes where the tape rule and the exchange agree or
disagree.

Demo books are thin and its traders are not production traders. Demo proves the mechanics, not the
edge. Exit criteria: 7 days, no reconciliation mismatch, no unknown order left open, every drill
passed.

## Stage 3: production (real money, owner only)

Only after stages 1 and 2 pass and every box in [LIVE_CHECKLIST.md](LIVE_CHECKLIST.md) is checked.

1. Lift the guard fully: delete the maker check in `kbot/app.py` (and its test). Commit it yourself.
2. A production key made only for this bot, `chmod 600`, in `KBOT_PROD_KEY_ID` and
   `KBOT_PROD_PRIVATE_KEY_PATH`. Fund the account with no more than you accept losing in full.
3. `config.maker-live.toml`: a copy of `config.maker.toml` with `mode = "production"` and
   `db_path = "data/live-maker.sqlite"`. Keep `bankroll = 25` and the default limits.
4. Arm in your own shell, never in a file the bot reads:
   ```bash
   export KBOT_MODE=production KBOT_PRODUCTION_ENABLED=true KBOT_PRODUCTION_ARM="I accept real-money risk"
   .venv/bin/python -m kbot check-auth
   .venv/bin/python -m kbot --config config.maker-live.toml run
   ```
5. First week: compare `status` with the Kalshi website every day. Any mismatch: `estop`, then
   investigate. Watch the fill model check. After 50 closed quotes with real orders, if the exchange
   filled fewer than half of the bids the tape rule filled, the paper results were too kind. Stop.

To disarm: `estop`, stop the process, `unset KBOT_PRODUCTION_ENABLED KBOT_PRODUCTION_ARM`.

## Known limits of the real-order path

* If the process dies without a clean shutdown, resting bids stay up until they expire at market
  close (at most 14 minutes). The risk limits already count them as open risk.
* A create that times out is marked unknown and never resent. Reconciliation resolves it. While it
  is unknown, no new bid goes on that side.
* The mocked tests use the request and response shapes in Kalshi's API docs. Kalshi has not seen
  these orders. Stage 2 is the first real check.

# Checklist before any real-money order

Production stays disabled until **every** box is checked by the account owner. The bot will not
start in production mode unless `KBOT_PRODUCTION_ENABLED=true` **and**
`KBOT_PRODUCTION_ARM="I accept real-money risk"` are both set, and it has no command that sets them.

## Evidence

- [ ] `kbot research` on at least 60 days of real data shows a strategy that PASSED validation
      (100+ trades, CI above zero, beats 95% of random runs, positive at +2 cents slippage).
- [ ] The final holdout was opened once (`holdout_opened_count == 1`) and the result is positive
      with a CI that does not include large losses.
- [ ] At least 14 days of continuous paper trading with 50+ settled trades. Paper mean net P&L per
      trade sits inside the backtest's 95% CI. Calibration gap under 2 standard errors.
- [ ] At least 7 days on the Kalshi demo account with no reconciliation mismatch, no unknown
      orders left unresolved, and fills close to the paper fill model.

## Operations

- [ ] All tests pass on the deployment machine: `.venv/bin/pytest`.
- [ ] Clock synced (NTP), skew under 2 s in `kbot status`.
- [ ] `kbot estop` and `kbot resume` rehearsed on demo, including cancellation of a resting order.
- [ ] Webhook notifications arrive on your phone (`notify_webhook`).
- [ ] Dashboard is local only, or protected by `KBOT_DASHBOARD_TOKEN` behind a VPN.

## Account and money

- [ ] A production API key created only for this bot, stored with `chmod 600`, in `KBOT_PROD_*`.
- [ ] The Kalshi account holds no more than you are willing to lose in full. The bot never
      deposits or withdraws.
- [ ] `[risk] bankroll` is at most $25 for the first live phase, and the other limits are the
      defaults or tighter.
- [ ] You have read `docs/REPORT.md` limitations, including that the fair-value model ignores
      jumps and fat tails.

## Arming (owner only, on the owner's machine)

```bash
export KBOT_MODE=production KBOT_DATA_ENV=production
export KBOT_PRODUCTION_ENABLED=true KBOT_PRODUCTION_ARM="I accept real-money risk"
kbot check-auth          # confirms production key and balance
kbot run
```

## First week live

- [ ] Check the dashboard daily. Compare with the Kalshi website positions and fills.
- [ ] Any reconciliation mismatch: `kbot estop`, investigate before resuming.
- [ ] Do not raise limits during the first 100 live trades. Never raise them to recover losses.

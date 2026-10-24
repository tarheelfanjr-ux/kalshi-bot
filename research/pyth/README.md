# Pyth fair value (E3) and the gold token stand-in

Rules: `spec.json`, frozen 2026-10-23 before any Pyth price was seen. Periods: `coverage.json`.
No rule here is used by the bot.

## E3 on Pyth: not run yet

Needs Pyth 1-minute bars, which need a Pyth key. The key stays in the local `.env` on the machine that
pulls the bars:

```
.venv/bin/python -m kbot --config research/pyth/config.pyth.toml collect-pyth --days 70
cd research/pyth && python run_e3.py valid && python run_e3.py test
```

`python run_e3.py control` checks the code on train with fake bars (bars that know the outcome must
light up, random bars must not). It passes.

## Gold with Coinbase PAXG-USD in place of Pyth (HISTORICAL TEST, 2026-10-24)

Cole chose to spend gold's validation and test periods on this stand-in (`spec.json`
`gold_proxy_holdout`). Fit on all of gold train (2,388 markets), each period read once.

| Measure | Validation (Sep 18 to Oct 6) | Test (Oct 6 to Oct 23) |
|---|---|---|
| Information gain over the mid (log-loss) | +0.0059 (CI +0.0004, +0.0110) | +0.0036 (CI -0.0012, +0.0083) |
| Taker net per contract | +2.8c (CI -0.4, +5.8), 724 trades | +5.4c (CI +2.2, +8.3), 713 trades |
| Book 60 s later | -0.3c | +2.1c |
| Token price 60 s old | -0.1c | +1.7c |
| Trades costing $0.25 or less | +4.1c, 77 trades | +10.4c (CI +1.0, +20.6), 71 trades |

**Verdict: fails the frozen pass rule**, on one clause: the test-period information gain's CI includes
zero. Every money clause passed on test. Under the rule, nothing here is re-tuned. Seen after the
fact and not a rule: the 5-minutes-left checkpoint carries most of the gain in all three periods
(information +0.010 each time).

```
KBOT_CAND_DB=pyth4-history.sqlite KBOT_FULL_DB=pyth4-full-candles.sqlite KBOT_PAXG_DB=paxg-1min.sqlite \
  python gold_proxy.py valid      # or test, or no argument for the train-only exploration
```

Data and full results: the project's `kalshi-bot/data` and `kalshi-bot/reports` folders.

## Gold paper test (rules fixed 2026-10-24, before it starts)

A prospective PAPER test of the same gold token rule on new markets. It never sends an order: the app
refuses `gold_token` in demo or production.

* **Run:** `python -m kbot --config config.gold.toml run` (database `data/paper-gold.sqlite`).
* **Data:** Kalshi books by public REST every 2 s (WebSocket if a read-only key is set), and the
  Coinbase PAXG-USD last price every 2 s. Needs no key.
* **Entry:** `kbot/gold.py`, frozen: checkpoints 600, 300, 120, 60 s before close, each taken once
  within 15 s after it; token scaled to the strike; single-print probability; blend coefficients fit on
  gold train; buy 1 contract when the blend beats ask + 1c + fee by more than 1c; one signal per
  market; book two-sided with spread at most 5c; skip with under 30 minutes of token data in the last
  hour or a token unchanged for 10 minutes. **Exit:** hold to settlement. Ties settle YES.
* **Research log:** every signal in the `signals` table (strategy `gold_token`), 1 contract at the
  top-of-book price seen at the decision. Net = win - (price + 1c) - cent-rounded taker fee.
* **Paper account:** $25 bankroll, 1% per position ($0.25), 5% open risk, 2% daily loss, 5 losses in a
  row, 3 open positions. Only signals costing about $0.23 or less can be placed, so the account's P&L
  is reported apart from the research log and is not the test.
* **Length:** starts at the first UTC midnight after the bot starts. Judged once after 14 days. With
  fewer than 300 signals by then, it runs to day 21 and is judged then.
* **Pass:** mean net per contract above 0 with its 95% CI (resampling whole settlement times) above 0,
  on at least 300 signals. Also reported: `gold_replay.py testB` on the same markets from minute
  candles. The gap between replay and live is what speed costs.
* **Stop or void:** risk halts and the emergency stop work as usual. If over 10% of checkpoints lack
  token data, or the bot is down more than 24 hours in total, the test is void and restarts at the next
  UTC midnight. Any change to code or thresholds starts a new test.
* **After a pass:** a longer paper run and the Pyth check come next. Nothing moves to real money from here.

Port check: replayed on gold test, the live rule makes the backtest's checkpoint and side on 629 of 713
signals and made +5.7c per contract (CI +2.7 to +8.7) on its own 737 signals. The differences come
from the backtest's stitched token series. Nothing was changed after this check.

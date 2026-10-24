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

# Metals, FX, Brent and Treasury-yield 15M markets: edge test (2026-10-23)

HISTORICAL BACKTEST on saved Kalshi data. Result: **no credible edge.** No rule here is used by the bot.

## Data

`candidates-history.sqlite` (project files): 18,075 settled markets in 13 series, 2026-08-27 to
2026-10-23 20:30 UTC, with minute candles. `fetch_full.py` re-downloads the same candles with every
field, because the resting-bid fill rule needs each minute's trade price low and high (checked equal
to the public trade tape on all 92 KX2YRRATE15M minutes that have both).

| Group | Series | Markets | Median spread | Median volume per market |
|---|---|---|---|---|
| Metals | copper, platinum, palladium | 3,885 / 2,323 / 2,323 | 2c / 3c / 3c | 12,721 / 4,677 / 4,670 |
| FX | EUR, GBP, JPY, CAD, AUD | 2,061 x3, 1,122 x2 | 2-3c | 1,505-4,002 |
| Brent | KXBRENT15M (from Oct 21) | 185 | 8c | 1,970 |
| Yields | 2Y, 5Y, 10Y, 30Y (from Oct 19) | 233 each | 25-28c | 0 (most minutes no trade) |

All series are fee_type `quadratic`: taker fee ceil(0.07 x C x P x (1-P)) per order, no maker fee.
Ties (close = strike) resolve YES.

## Method

* Periods per series: **train** = the exploration half named in `candidates-coverage-2026-10-23.json`;
  the rest split in half by count: **valid** (earlier), **test** (later, untouched until the final run).
* `spec.json` was frozen (sha256 prefix `9ec48dbaa06f`) after `explore_train.py` and before any
  validation or test outcome was read. It discloses what was tried on train.
* 1 contract per market per rule, held to settlement.
* Taker fills: the ask (or 1 - bid) at the decision minute's close **plus 1c slippage**, cent-rounded
  1-contract fee. Stress: the next minute's book.
* Resting bids: no fee; fill only when a later public trade prints **through** our price (at or
  through for the 1c-improved bid, which is alone at the front). Stresses: a 0.0175 maker fee,
  strictly-through fills for the improved bid, fills from the second minute only.
* Uncertainty: 95% bootstrap CI resampling whole settlement times (markets that close together).

## Results, test period (net per contract, 95% CI, trades)

| Rule | Metals | FX | Brent | Yields |
|---|---|---|---|---|
| T1 coin-flip taker, minute 1 | -3.1c (-5.2, -1.1) 2,133 | -3.1c (-5.2, -1.1) 2,110 | -7.7c 47 | -9.5c 232 |
| T2 YES taker, minute 1 (ties) | -7.9c (-10.4, -5.3) 2,133 | -2.7c (-4.6, -0.7) 2,110 | -0.3c 47 | -11.4c 232 |
| T3 favorite taker, minute 5 | -1.0c (-3.5, +1.5) 1,351 | -4.5c (-7.3, -1.9) 1,377 | -5.6c 35 | -9.0c 148 |
| M2 bid both sides, minute 1 | -2.6c (-3.1, -2.1) 3,921 | -3.7c (-4.3, -3.2) 3,808 | -0.4c 87 | -3.3c 228 |
| M1 join favorite bid, minute 5 | +0.4c (-2.5, +3.2) 1,131 | -5.0c (-8.4, -1.8) 1,126 | -2.0c 27 | -8.6c 57 |
| **M3 favorite bid +1c, minute 3** | **+0.5c (-3.0, +3.9) 737** | -4.2c (-7.5, -0.8) 952 | +0.1c 26 | -5.3c 62 |

Primary (M3 on metals): train +2.7c (picked as the best of three start minutes), valid +0.0c
(-3.5, +3.3), test +0.5c. Test by series: copper +3.3c, palladium -2.5c, platinum -0.7c. Under
stress the test result turns negative (maker fee -0.5c, strictly-through fills -0.2c). Fails the
frozen pass rule. Valid + test pooled: +0.2c (-2.2, +2.8) over 1,492 fills.

Full tables by series, stresses and drawdowns: `metals-fx-results-*-2026-10-23.json` in the
project's `kalshi-bot/reports/` folder.

## Reproduce

```
pip install pandas numpy            # research only, not a bot dependency
export KBOT_CAND_DB=path/to/copy-of/candidates-history.sqlite
export KBOT_FULL_DB=path/to/candidates-full-candles.sqlite
export KBOT_COVERAGE=path/to/candidates-coverage-2026-10-23.json
cd research/metals_fx
python fetch_full.py                # about 15 minutes at the public rate limit
python inventory.py                 # outcome-free
python explore_train.py             # train half only
python run_frozen.py valid
python run_frozen.py test
python common.py                    # self-check of the fill rules and fees
```

Every saved market has now been read. A new test on these series needs markets that close after
2026-10-23 20:30 UTC.

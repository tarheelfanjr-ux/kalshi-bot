# Development plan and acceptance criteria

## Assumptions (documented defaults)

* Python 3.11+ (3.13 used), one asyncio process, SQLite in WAL mode.
* Taker-only entries with immediate-or-cancel orders. Nothing rests on the book by
  accident, and maker fill modelling (queue position) is avoided until there is data for it.
* Hold to settlement. No exits, no reversals, one position per market. This removes exit
  slippage and keeps P&L tied to the official settlement result.
* Whole contracts only. Kalshi count fields accept 2 decimals, but fractional support for
  15-minute markets has not been verified.
* Fees are recomputed from the series `fee_type` and `fee_multiplier` and rounded up to the
  cent per order, which is at least as large as Kalshi's per-fill rounding.

## Strategy design

The 15-minute crypto contracts settle YES if the 60-second average of a CF Benchmarks index
at the end of the window is at or above the reference set at the start (`floor_strike`).
That is a short-horizon digital option. Candidates, in order of simplicity:

| # | Strategy | Probability estimate | Data needed |
|---|---|---|---|
| 0 | `no_trade` | none | none |
| 1 | `market_implied` | market mid | Kalshi book |
| 2 | `momentum` | mid + 0.5 x 3-min mid move | Kalshi book |
| 3 | `mean_reversion` | mid - 0.5 x 3-min mid move | Kalshi book |
| 4 | `fair_value` | Brownian model: spot vs strike, realized volatility, time left, 60 s averaging | Kalshi book + index price |

`fair_value` is the main hypothesis: when the index moves late in a window, the Kalshi
quote may lag the arithmetic of the settlement rule. Benchmarks 0 to 3 and a random-direction
baseline (same timing and size, coin-flip side) show whether any edge is real.

Every candidate must clear: `q_win - ask - fee - model_uncertainty >= min_net_edge` (3 cents
by default), with spread <= 6 cents, at least 5 contracts at the top level, and 90 to 840
seconds left.

## Test method

1. Collect every settled 15-minute market with Kalshi minute candles (top-of-book bid/ask).
2. Split chronologically: 60% train, 20% validation, 20% final holdout. The holdout opens
   only with `--open-holdout`, and each opening is counted and reported.
3. Walk-forward: four consecutive blocks over train + validation.
4. Fills: IOC at the ask at decision time plus 1 cent slippage, 80% fill probability, at
   most 5 contracts per order (candles show no depth). Stress: 2 and 3 cents slippage, 50%
   fills, 1.5x fees.
5. Confidence intervals: bootstrap that resamples whole settlement times, so BTC and ETH
   windows closing together count as one cluster.

**Pass rule (validation):** at least 100 trades, cluster-bootstrap 95% CI of mean net P&L per
trade above zero, beats at least 95% of random-direction runs, still positive with 2 cents
extra slippage. Then the holdout is opened once. Then at least 2 weeks of paper trading with
results inside the backtest's CI. Then demo. Only then the live checklist.

## Acceptance criteria (non-live)

| # | Criterion | How it is checked |
|---|---|---|
| 1 | Starts from a documented command | `python -m kbot run` |
| 2 | Connects to intended environment | `kbot check-auth`, `kbot discover` |
| 3 | Discovers eligible markets | `kbot discover`; `test_paper_tick_*` |
| 4 | Fresh market data | health check rejects books older than 10 s; `test_stale_book_blocks_entry` |
| 5 | Evaluates candidates | `signals` table; strategy tests |
| 6 | Net profitability after costs | `Candidate.net_edge`; fee tests |
| 7 | Records why it trades or not | `signals.reason`, `events` |
| 8 | Historical backtesting | `kbot backtest`, `kbot research` |
| 9 | Persistent paper trading | SQLite state; restart test |
| 10 | Survives network interruptions | retry/backoff, WS reconnect; run-loop test |
| 11 | Restores state after restart | `test_restart_does_not_duplicate_or_reopen` |
| 12 | Reconciles orders, fills, positions, settlements | reconciliation tests |
| 13 | Risk controls tested | `test_risk_execution.py` |
| 14 | Emergency stop tested | `test_estop_and_pause_block_new_orders` |
| 15 | Dashboard shows accurate info | `test_paper_tick_*` checks dashboard state |
| 16 | Credentials stored securely | env vars + chmod 600 key files; config rejects secrets |
| 17 | Production disabled until approved | client and config tests |
| 18 | Documentation | this folder |
| 19 | All tests pass | `pytest` |
| 20 | Results labelled | `reports.LABELS`, research label |

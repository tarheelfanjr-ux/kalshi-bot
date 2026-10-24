# Status report (updated 2026-10-23, real-data backtest)

## Bottom line

**A 90-day backtest on real Kalshi data found no profitable strategy. Do not paper or live trade
momentum or mean reversion.** fair_value is still untested because this environment blocks every
spot price feed. Production trading is disabled and no orders were sent.

All numbers below are HISTORICAL BACKTEST (simulated fills on Kalshi minute candles).

* Data: 34,128 settled markets (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M), 2026-07-25 14:30 to
  2026-10-23 14:30 UTC, 477,435 minute decision points. 34,123 results match the settlement rule,
  0 mismatches, 5 not checkable. Only gaps are 13 Thursday maintenance windows. 24 markets had no candles.
* Split by time: train Jul 25 to Sep 17 (20,462 markets), validation Sep 17 to Oct 5 (6,821),
  holdout Oct 5 to Oct 23 (6,821). Holdout opened once per run below.

| Run | Strategy | Train | Validation | Holdout |
|---|---|---|---|---|
| $25 bankroll (configured) | mean_reversion | 386 trades, -$13.26, max DD $19.75 | 170 trades, -$1.80, CI/trade [-0.063, +0.045] | 135 trades, -$0.68, CI/trade [-0.080, +0.090] |
| $25 bankroll (configured) | momentum | 6 trades, -$0.44 | 2 trades, -$0.50 | 8 trades, +$0.05 |
| $1,000 diagnostic | momentum | 5,284 trades, -$788.85 | 1,922 trades, -$238.15, CI/trade [-0.240, -0.008] | 1,718 trades, -$255.06, CI/trade [-0.281, -0.024] |
| $1,000 diagnostic | mean_reversion | 769 trades, -$129.40 | 269 trades, -$41.82 | 268 trades, -$36.99 |

fair_value, market_implied, and no_trade made 0 trades in every period (fair_value: no spot data;
market_implied: correct, costs leave no free edge). Full reports: `reports/research-A-configured.json`,
`reports/research-B-diagnostic.json`, `reports/data-verification-90d.json`.

Remaining restrictions: WebSocket returns 401 without an API key. Coinbase and every other spot
source are blocked by the environment's network policy. Unauthenticated Kalshi reads are throttled
near 3.5 req/s.

### Maker strategy (added 2026-10-23, after the audit)

E2's maker rule (bid at the best YES and NO bid each minute, fills only on trades through our
price) also has no edge so far. HISTORICAL BACKTEST on a seeded sample of 1,298 markets that closed
before the holdout: 2,596 fills, -1.82 cents per contract before fees (95% CI -2.06 to -1.59),
negative in every series, both halves and every latency variant. Markets where both sides filled
on the first quote earned +1.28 cents per pair (63%). The rest lost 12.06 cents per pair (37%).
Report: `reports/maker-history-2026-10-23.json` in project files. E2's own frozen test on data after
Oct 24 is unchanged. The maker bot runs in paper mode. Go/no-go and next steps: [GO_LIVE.md](GO_LIVE.md).

## Phase reports

| Phase | Implemented | Tested | Passed | Failed / incomplete |
|---|---|---|---|---|
| Research | Official docs read for auth, endpoints, V2 orders, books, fees, rate limits, lifecycle, historical split, WebSocket channels | n/a | n/a | API reachable since 2026-10-23. `kbot discover` found 29 live 15M series. |
| 1. Quant research | Calibration of market mid vs outcome, strategy comparison, random-direction baseline, sensitivity, cluster bootstrap CIs, holdout control, pass/fail verdict | End-to-end on hand-made test input | Yes | Run on 90 days of real data 2026-10-23. No strategy passed. |
| 2. Discovery | 15M series scan, window check, eligibility, lifecycle events, REST + WS, rate limiter, reconnect | Unit + app tests | Yes | Live discover works (2026-10-23). WebSocket untested (needs key). |
| 3. Signals | `Candidate` with all requested fields, YES/NO executable prices, fees, uncertainty, invalidation, 4 strategies + no-trade | Unit tests | Yes | Fair-value model ignores drift, jumps, fat tails. |
| 4. Backtest | Event-driven replay through real risk and execution code; fees, slippage, partial and missed fills, capital, limits, settlement; metrics incl. Sharpe, drawdown, streaks, fee burden, by series and volatility regime | Hand-checked P&L, no-lookahead spy, stress, daily-loss trip | Yes | Kalshi candles have no depth, so size per order is capped by assumption. No historical book reconstruction. |
| 5. Paper | Live data, simulated IOC fills at 50% of displayed size, fees, positions, settlement, P&L, persistence, labelled results | App tests incl. restart | Yes | Not run against live data. |
| 6. Account | Separate public / account / trading clients, Ed25519 + RSA-PSS signing, demo/prod key separation, production arming, cancel-only client | Signature verification, body shape, guards | Yes | Demo integration not run (needs network + your demo key). |
| 7. Execution | State machine, durable intent, no resend on timeout, client order ids, reconciliation, partial fills, cancels, insufficient funds halt | Unit tests | Yes | Fee and cost fields on real responses unverified. |
| 8. Risk | Central gate with every requested default; worst-case sizing; resting + partial orders counted; persistent daily ledger; review after loss streak | Unit tests incl. restart | Yes | none known |
| 9. Operation | Async loop, graceful shutdown, health (freshness, API, clock, WS, reconcile age), integrity check, crash detection, backups, pause, e-stop | App tests incl. network failure | Yes | Long soak test not run. |
| 10. Dashboard | Web control center: every requested panel, mode labels, paper-only start, pause, resume, stop, e-stop, settings, backtests; token for non-local binding | `tests/test_web.py` (CSRF, origin, host, login, no secrets, controls); run against the live paper bot | Yes | Start/stop detection uses `ps` (macOS, Linux) |

## Results

| Category | Status |
|---|---|
| Historical backtest | Run on 90 days of real data (2026-10-23). No strategy passed. See bottom line. |
| Paper trading | Maker bot running on live data since 2026-10-23 18:48 UTC (short test, see GO_LIVE.md). |
| Kalshi demo account | **Not run.** Needs your demo key. The maker order path is tested against a mocked exchange only. |
| Production | Disabled. Never run. |

## What the research already says without data

* Fees peak at 1.75 cents per contract at 50 cents and are rounded up per order, so a single
  contract near 50 cents costs 2 cents. With a typical 1 to 2 cent spread, any strategy needs
  roughly 3 to 4 cents of true edge per contract near the middle just to break even.
* The `market_implied` benchmark can never trade after costs (tested). A positive result for it
  would mean a bug.
* With the requested $25 bankroll and 1% per-position risk, the worst-case loss per position is
  $0.25. That allows one contract priced at 23 cents or less. Contracts near 50 cents cannot be
  traded at all under these limits. This is a hard constraint for live trading, not a bug.
* BTC, ETH, SOL and other windows that close at the same moment move together. Treating them as
  independent would overstate confidence, so the bootstrap clusters them.

## Risk and security audit

| Area | Finding |
|---|---|
| Real-money orders | Only `TradingClient.create_order` can place one. It refuses production unless config has `mode=production`, `production_enabled=true`, and the exact arm phrase from env. Paper mode has no trading client at all (tested by blocking every POST). |
| Retries | Reads retry with backoff. Order creates never retry. A timeout or 5xx becomes `unknown` and is reconciled by client order id. |
| Duplicates | The order row is written before sending. Unknown and open orders mark the market busy, so no second order goes in. |
| Restart | Crash detection on startup, reconciliation before trading in demo/prod, daily loss read from the persistent ledger. |
| Loss controls | Daily loss, loss streak, mismatch, insufficient funds, and e-stop all halt entries and require `kbot resume --reviewed`. Limits are never raised by code. |
| Secrets | Keys only from env-specified files with mode 600. Config file rejects secret keys. Signer repr masks the key id. Dashboard output tested for leaks. `.gitignore` covers `.env`, keys, `data/`, `reports/`. |
| Scope | No transfer, withdrawal, deposit, subaccount, or API-key endpoints are implemented. |
| Dashboard | Localhost by default, token login required for other bindings, CSRF and origin checks on every control. Starts paper mode only and strips trading keys from the bot it starts. No way to arm production or place an order. |
| LLM / ML | No model can authorize trades. Every order passes the deterministic risk gate. |
| Known residual risks | Fill-cost field semantics on real V2 responses are inferred from docs; position reconciliation is the backstop. WS resubscribes by reconnecting when the market set changes (about every 15 minutes), leaving a brief gap the freshness check covers. |

## Limitations

1. Real market data is reachable now, but spot prices, the WebSocket, and account endpoints are not.
   Acceptance criteria that need them (3 live, 4 live, 10 live, 12 live) are verified only against mocks.
2. Kalshi's historical candles have top-of-book only. Depth, queue position, and latency are
   assumptions. The bot records live book snapshots every 10 s so future backtests can use real depth.
3. The CF Benchmarks history endpoint needs an entitlement. Coinbase prices are a proxy for the
   settlement index and can differ from it, especially in fast markets.
4. Index ids other than `BRTI` (e.g. `ETHUSD_RTI`) follow the documented naming pattern but were not confirmed.
5. Only crypto series get a fair-value model. Pyth-settled metals, energy, and FX series are discovered but only market-based strategies apply.
6. Whole contracts only, taker IOC only, hold to settlement.
7. Sharpe is computed from daily P&L, assumes independent days, and means little with few trading days or tiny positions.
8. No long soak test of the live loop.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Starts from documented command | Met (`kbot run`, smoke tested) |
| 2 | Connects to intended environment | Met for public REST and historical data (2026-10-23). WebSocket needs a key |
| 3 | Discovers eligible markets | Met on mocks; live unverified |
| 4 | Fresh market data | Met on mocks |
| 5 | Evaluates candidates | Met |
| 6 | Net profitability after costs | Met |
| 7 | Records why it trades or not | Met |
| 8 | Historical backtesting | Met. Real-data run 2026-10-23: no strategy passed |
| 9 | Persistent paper trading | Met on mocks |
| 10 | Survives network interruptions | Met on mocks |
| 11 | Restores state after restart | Met |
| 12 | Reconciles orders, fills, positions, settlements | Met on mocks |
| 13 | Risk controls tested | Met |
| 14 | Emergency stop tested | Met |
| 15 | Dashboard accurate | Met on mocks |
| 16 | Credentials stored securely | Met |
| 17 | Production disabled until approved | Met |
| 18 | Documentation | Met |
| 19 | All tests pass | Met (75/75) |
| 20 | Results labelled | Met |

## Next concrete task

Get spot data so fair_value can be tested: allow api.exchange.coinbase.com in the environment's
network settings, or import licensed CF Benchmarks history with `kbot import-spot`. Then run
`kbot collect-spot --days 90` and `kbot research`. The Oct 5 to Oct 23 holdout has now been opened,
so judge any newly tuned strategy on data after 2026-10-23.

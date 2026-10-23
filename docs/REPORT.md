# Status report (2026-10-23)

## Bottom line

The full system is built and its automated tests pass (71 of 71, against a mocked exchange).
**No strategy has been tested on real data, so there is no evidence of an edge yet.** The
cloud machine it was built on blocks every Kalshi host and every crypto price feed, so no
market history could be downloaded. Nothing below is a performance result. Production trading
is disabled.

The next step is to run `kbot collect`, `kbot collect-spot`, and `kbot research` from a machine
that can reach Kalshi. The research verdict decides whether anything moves to paper trading.

## Phase reports

| Phase | Implemented | Tested | Passed | Failed / incomplete |
|---|---|---|---|---|
| Research | Official docs read for auth, endpoints, V2 orders, books, fees, rate limits, lifecycle, historical split, WebSocket channels | n/a | n/a | Could not call the API. The list of 15M series is from a third-party site and must be confirmed with `kbot discover`. |
| 1. Quant research | Calibration of market mid vs outcome, strategy comparison, random-direction baseline, sensitivity, cluster bootstrap CIs, holdout control, pass/fail verdict | End-to-end on hand-made test input | Yes | **No real data: no research results exist.** |
| 2. Discovery | 15M series scan, window check, eligibility, lifecycle events, REST + WS, rate limiter, reconnect | Unit + app tests | Yes | Live API untested. |
| 3. Signals | `Candidate` with all requested fields, YES/NO executable prices, fees, uncertainty, invalidation, 4 strategies + no-trade | Unit tests | Yes | Fair-value model ignores drift, jumps, fat tails. |
| 4. Backtest | Event-driven replay through real risk and execution code; fees, slippage, partial and missed fills, capital, limits, settlement; metrics incl. Sharpe, drawdown, streaks, fee burden, by series and volatility regime | Hand-checked P&L, no-lookahead spy, stress, daily-loss trip | Yes | Kalshi candles have no depth, so size per order is capped by assumption. No historical book reconstruction. |
| 5. Paper | Live data, simulated IOC fills at 50% of displayed size, fees, positions, settlement, P&L, persistence, labelled results | App tests incl. restart | Yes | Not run against live data. |
| 6. Account | Separate public / account / trading clients, Ed25519 + RSA-PSS signing, demo/prod key separation, production arming, cancel-only client | Signature verification, body shape, guards | Yes | Demo integration not run (needs network + your demo key). |
| 7. Execution | State machine, durable intent, no resend on timeout, client order ids, reconciliation, partial fills, cancels, insufficient funds halt | Unit tests | Yes | Fee and cost fields on real responses unverified. |
| 8. Risk | Central gate with every requested default; worst-case sizing; resting + partial orders counted; persistent daily ledger; review after loss streak | Unit tests incl. restart | Yes | none known |
| 9. Operation | Async loop, graceful shutdown, health (freshness, API, clock, WS, reconcile age), integrity check, crash detection, backups, pause, e-stop | App tests incl. network failure | Yes | Long soak test not run. |
| 10. Dashboard | Local read-only page with every requested panel, mode labels, token for non-local binding | Smoke tested; no-secrets test | Yes | none known |

## Results

| Category | Status |
|---|---|
| Historical backtest | **Not run on real data.** Blocked by network. |
| Paper trading | **Not run.** Blocked by network. |
| Kalshi demo account | **Not run.** Needs your demo key and network. |
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
| Dashboard | Read-only, localhost by default, token required for other bindings. No order controls. |
| LLM / ML | No model can authorize trades. Every order passes the deterministic risk gate. |
| Known residual risks | Fill-cost field semantics on real V2 responses are inferred from docs; position reconciliation is the backstop. WS resubscribes by reconnecting when the market set changes (about every 15 minutes), leaving a brief gap the freshness check covers. |

## Limitations

1. No real data was collected or tested (network policy). All acceptance criteria that need the
   live API (2, 3 live, 4 live, 10 live, 12 live) are verified only against mocks.
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
| 2 | Connects to intended environment | **Not verified** (network blocked); code and URLs per docs |
| 3 | Discovers eligible markets | Met on mocks; live unverified |
| 4 | Fresh market data | Met on mocks |
| 5 | Evaluates candidates | Met |
| 6 | Net profitability after costs | Met |
| 7 | Records why it trades or not | Met |
| 8 | Historical backtesting | Met (code); **no real-data run** |
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
| 19 | All tests pass | Met (71/71) |
| 20 | Results labelled | Met |

## Next concrete task

On a machine with Kalshi access: `kbot discover`, then `kbot collect --days 60`,
`kbot collect-spot --days 60`, `kbot research`. Start `kbot run` in paper mode at the same time so
live book snapshots begin accumulating.

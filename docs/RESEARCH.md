# Research notes (verified 2026-10-23)

Everything here was read from official Kalshi documentation on 2026-10-23 unless marked
"third party". The bot re-reads fee settings, rules, and strikes from the live API at
runtime, so these notes are a starting point, not hardcoded truth.

## Environment notes

The first build environment blocked all Kalshi hosts. Since 2026-10-23 the project environment
reaches Kalshi REST, historical data, and Coinbase Exchange. The WebSocket returns 401 without a key,
and other spot sources (Kraken, Binance, CF Benchmarks) are still blocked. Unauthenticated Kalshi reads
are throttled near 3.5 req/s. Backtest results are in `docs/REPORT.md`.

## API facts

| Topic | Fact | Source |
|---|---|---|
| Prod REST | `https://external-api.kalshi.com/trade-api/v2` (alt `api.elections.kalshi.com`) | docs.kalshi.com/getting_started/api_environments |
| Demo REST | `https://external-api.demo.kalshi.co/trade-api/v2` | same |
| Prod WS | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | same |
| Demo WS | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | same |
| Credentials | Not shared between demo and prod | same |
| Auth headers | `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE` (base64) | getting_started/api_keys |
| Signed string | `timestamp + METHOD + path` (path without query) | same |
| Key types | Ed25519 (recommended) or RSA-2048 with RSA-PSS/SHA-256, MGF1 SHA-256, salt 32 | same |
| WS auth | Same headers on handshake, sign `GET /trade-api/ws/v2` | quick_start_websockets |
| Rate limits | Basic tier: 200 read tokens/s, 100 write tokens/s, most requests cost 10 tokens. Cancel costs 2. Balance has its own 1 req/s bucket. | getting_started/rate_limits, changelog 2026-10-15 |
| Prices | Fixed-point dollar strings (`*_dollars`, up to 4 dp). Counts are `*_fp` strings (2 dp). Cent fields removed 2026-04-02. | fixed_point_migration, changelog |
| Order book | `GET /markets/{t}/orderbook` returns only bids: `orderbook_fp.yes_dollars`, `no_dollars`, ascending. YES ask = 1 - best NO bid. | orderbook_responses |
| Create order | `POST /portfolio/events/orders`: `ticker, side (bid/ask on YES leg), count, price, time_in_force (fill_or_kill/good_till_canceled/immediate_or_cancel), self_trade_prevention_type`, optional `client_order_id`, `post_only`, `reduce_only` (IOC only), `cancel_order_on_pause`. Response has `order_id, fill_count, remaining_count, average_fill_price, average_fee_paid`. | api-reference/orders/create-order-v2 |
| Buy NO | `book_side=ask` on the YES leg at price `1 - no_price` | getting_started/order_direction |
| Cancel | `DELETE /portfolio/events/orders/{id}?market_ticker=...` | cancel-order-v2 |
| Order lookup | `GET /portfolio/orders/{id}`, list `GET /portfolio/orders?ticker=`. No filter by `client_order_id`, so the bot filters client side. | get-order, get-orders |
| Positions | `GET /portfolio/positions`, `position_fp` positive = YES, negative = NO | get-positions |
| Fills | `GET /portfolio/fills` with `outcome_side, book_side, count_fp, yes_price_dollars, fee_cost, is_taker` | get-fills |
| Historical | Old settled markets, trades, candles move to `/historical/*` after `GET /historical/cutoff` | getting_started/historical_data |
| Candles | `GET /series/{s}/markets/{t}/candlesticks?period_interval=1` gives per-minute yes_bid/yes_ask OHLC and volume. No depth. | get-market-candlesticks |
| Market status | `initialized, active, inactive(paused), closed, determined, disputed, amended, finalized` | market_lifecycle |
| Spot index | WS channel `cfbenchmarks_value` (e.g. `BRTI`) with a 60 s average field; REST passthrough `/cfbenchmarks/history/values` needs an entitlement and costs 50 tokens | websockets/cfbenchmarks-value, cfbenchmarks/rest-passthrough |

## Fees

Kalshi fee schedule PDF (effective 2026-07-07):

* Taker: `ceil(M x 0.07 x C x P x (1 - P))`
* Maker: `ceil(M x 0.0175 x C x P x (1 - P))` where maker M defaults to 0 unless the series says otherwise.
* No settlement fee.

The series object carries `fee_type` (`quadratic`, `quadratic_with_maker_fees`,
`quadratic_with_combo_maker_fees`, `flat`) and `fee_multiplier`. Upcoming changes are at
`GET /series/fee_changes`. Changelog says maker fees were enabled 2026-08-20.
Per-fill rounding: fee rounded up to $0.000001, balance change rounded down to the
member's precision ($0.01 for non-direct members), with a rebate accumulator
(getting_started/fee_rounding). **The bot rounds every fee up to the cent and assumes
the taker rate, which is slightly pessimistic.**

## 15-minute series (third party, to be confirmed by `kbot discover`)

predictionmarketspicks.com lists 20 trading 15-minute series: crypto (KXBTC15M,
KXETH15M, KXSOL15M, KXXRP15M, KXDOGE15M, KXHYPE15M, KXZEC15M, KXNEAR15M, KXBNB15M,
KXCRYPTOLEAD15M) settling on CF Benchmarks real-time indices, and Pyth-settled metals,
energy, and FX (KXGOLD15M, KXSILVER15M, KXWTI15M, KXEURUSD15M ...). Crypto contracts
are "up or down": YES if the 60-second average of the index at the end of the window is at
or above the reference set at the start (`floor_strike`). Discovery does not trust this
list. It scans `GET /series` for tickers ending in `15M` and confirms that markets close
about 15 minutes after they open.

## What this implies for strategy

1. Each 15-minute window per asset is one independent observation. BTC and ETH windows
   that close at the same time are correlated, so the backtester clusters by close time.
2. Kalshi's own historical candles give top-of-book bid/ask per minute plus the result.
   This supports a conservative taker backtest but has no depth, so fill size is capped.
3. A fair-value model needs the underlying index path. Kalshi's CF Benchmarks history
   needs an entitlement. Without it, the live recorder must collect spot and book data
   going forward before the model strategy can be tested honestly.
4. Fees are largest at P = 0.5 (1.75 cents per contract at taker rate). Any edge under
   about 2 to 3 cents per contract near the middle is gone after fees and spread.

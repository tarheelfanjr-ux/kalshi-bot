# Prospective experiments E1 and E2 (frozen 2026-10-23)

fair_value is retired. The 2026-10-23 audit (`kbot audit`) named two hypotheses. Each one gets
its own experiment, judged once on data recorded after 2026-10-23. Full reasoning, feasibility,
costs and the ranking: [Kalshi 15M new edge research](https://claude.ai/artifact/CHVbiiKDVhY1N13gY23AFc).

No results exist yet. Production stays disabled. Nothing here places an order.

| | E1: real index feed | E2: maker orders |
|---|---|---|
| Question | Does the CF RTI beat the Kalshi mid by more than a taker's costs? | Do 1-contract bids resting at the best bid earn after adverse selection? |
| Data | RTI ticks (key needed), books, Coinbase ticks. Recorded live only. | Settled markets, minute candles, trades. Downloadable later. |
| Period | D0 (first UTC day with RTI ticks) + 14 days fit, then 42 days evaluation | 2026-10-24 to 2026-12-19 |
| Looks | Final only | One futility look after 2026-11-22, then final |
| Pass rule | 97.5% CI above zero for log loss gain and for net profit per trade, plus stress and stability checks | 97.5% CI above zero for net profit per fill, plus stress and stability checks |

The specs live in `SPECS` in `kbot/experiments.py`. Each report prints the spec hash. Changing a
spec after its start makes a new experiment with a new start date.

Rules the code enforces:

- Markets that opened before 2026-10-24 00:00 UTC are never read. The 90-day file is never opened.
- A decision at time t uses only data received locally by t.
- `kbot experiment status` shows coverage only, never outcomes.
- `kbot experiment evaluate NAME --final` refuses before the end date and counts every opening.
  `--force` reads early, is counted, and is labelled not valid.
- If coverage is under 70%, the end date moves out. Results never move it.
- E1 and E2 are judged separately. No combined strategy until both have final results.

Running it: see [OPERATING_MANUAL.md](OPERATING_MANUAL.md#prospective-experiments).

The maker paper bot (`config.maker.toml`) runs E2's rule live. Its 14-day run is test P1 below. P1
shares E2's data from Oct 24 on but not its verdict: E2 is still read once, by its frozen rule.

## P1: the 14-day paper test of the maker bot (frozen 2026-10-23)

P1 asks one question: does E2's rule, placed under the configured $25 paper limits, earn after fees
on fresh live data? The spec is `PAPER_TEST` in `kbot/maker.py`. `kbot --config config.maker.toml
status` prints its hash, its state and every check. Changing the spec or the limits makes a new test.

**Which variant, and why.** Cole asked for first-minute quotes only if the existing evidence supports
them. One check on the same 1,298 pre-holdout markets as the saved history (seed 7, no holdout data):

| Rule | Fills | Net per contract (95% CI) |
|---|---|---|
| Every minute from 840 s to 60 s left (E2) | 2,596 | -1.82 c (-2.06 to -1.59) |
| First-minute quote only (840 s left) | 2,117 | -1.91 c (-2.95 to -0.88) |
| Bids at 0.25 or less only, E2 | 61 | -7.93 c (-15.83 to +0.89) |
| Bids at 0.25 or less only, first minute | 56 | -6.77 c (-15.29 to +3.00) |

First-minute quotes do no better. Both sides fill on the first quote in 63% of markets, but the rest
lose more than those pairs earn. So P1 keeps E2's rule unchanged. No other variant was tried. The
last two rows are roughly what the $25 limits allow (at most $0.25 at risk per position), and they
are worse. This is evidence against the rule, recorded before P1 starts.

**Rules (all fixed).**

- Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M.
- Entry: at each minute from 840 s to 60 s before close, a 1-contract bid at the best YES bid and
  one at the best NO bid, prices 0.05 to 0.95, each resting until the next minute.
- Fills: a bid fills only when a public trade prints through its price at least 1 s after posting.
  A trade at our price never fills it. At most one fill per side per market.
- Exit: none. Every fill is held to settlement.
- Fees: the series' own fee (quadratic, so zero for makers). Stress case: 0.0175 x p x (1 - p).
- Slippage: none. A maker fill is at the bid price or not at all.
- Risk limits: bankroll $25, 1% worst-case loss per position, 5% open risk, 2% daily loss, halt after
  5 losses in a row, at most 3 open markets, 1 contract per market side. These are the limits the
  bot runs with. They are not raised to let the account hold both sides.

**Two results, kept apart.**

- **Executable (primary):** the paper account, which places only what the limits allow. Net per
  settled contract with a 95% CI over whole windows.
- **Research only:** the research log, every quote at 1 contract with no limits. It shows whether
  the rule earns at all. It is not a result anyone could have traded at $25.

**Start, length, stops.**

- Start: the bot's first minute running on a fresh database, created after this spec. Nothing from
  the cloud runs of 2026-10-23 counts.
- Length: 14 days, plus the length of every gap over 10 minutes, so downtime never shortens it. A
  gap is a stretch with no per-minute equity row from the bot.
- Void: any gap over 2 hours. Start again with a fresh database.
- Early stop: if the account's net loss reaches $5 (20% of the bankroll), P1 ends as a FAIL.
- Risk halts work as configured. The owner resumes them after review. Halted time is reported.
- No tuning: nothing in the spec, the code paths it uses or the limits changes during the test.
  Every startup logs the limits it runs with, and `status` flags any change.

**Pass, read once at the end.** All of these:

- Account: 50+ settled trades, 95% CI above zero, mean above zero at the stress fee.
- Research log: 300+ fills over 100+ windows, 95% CI above zero, mean above zero at the stress fee.
- No gap over 2 hours, and the risk limits unchanged for the whole test.

A PASS allows stage 2 (demo) in [GO_LIVE.md](GO_LIVE.md). It is not evidence of a real-money edge:
paper fills assume we are at the back of the queue, and real queue position is unknown.

## Candidate series: recorded, not frozen

From 2026-10-23 the recorder also records books, settled markets, candles, trades and fee state for
13 more 15M series: KXCOPPER15M, KXPLATINUM15M, KXPALLADIUM15M, KXEURUSD15M, KXGBPUSD15M,
KXUSDJPY15M, KXUSDCAD15M, KXAUDUSD15M, KXBRENT15M and KX2YRRATE15M, KX5YRRATE15M, KX10YRRATE15M,
KX30YRRATE15M (`CANDIDATE_SERIES` in `kbot/recorder.py`). E1 and E2 never read them.

No experiment on these series is frozen yet. Candidate ideas are in the
[thin-series scan](https://claude.ai/artifact/FBUQNh3X2giTnj1QME4UVa). For any past-data test, only
the earliest 50% of each series' settled markets (as of 2026-10-23) has been read, and only for its
tie rate. The later half and every mid-against-outcome comparison stay unread until a spec is frozen.

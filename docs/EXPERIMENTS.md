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

The maker paper bot (`config.maker.toml`) runs E2's rule live as a short, labelled paper test. It
shares E2's data from Oct 24 on but not its verdict: E2 is still read once, by its frozen rule.

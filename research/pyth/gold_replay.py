"""Replay kbot/gold.py (the live paper rule) on minute candles and compare it with gold_proxy.py.

Shows how often the live port makes the backtest's decision (same checkpoint and side) and what its own
signals made, priced like the backtest (ask or 1 - bid at the checkpoint + 1c, cent-rounded fee).
Use it on testB (markets from Oct 24) next to the live paper log to see what latency costs.

    KBOT_CAND_DB=... KBOT_FULL_DB=... KBOT_PAXG_DB=... python gold_replay.py test | testB
"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

import run_e3 as e3
from common import boot_ci, fee1, panel

from kbot.book import OrderBook  # noqa: E402
from kbot.config import StrategyConfig  # noqa: E402
from kbot.discovery import MarketInfo  # noqa: E402
from kbot.gold import CHECKPOINTS, GoldToken  # noqa: E402
from kbot.strategy import Candidate, Snapshot  # noqa: E402

P = sys.argv[1]
m, cd = panel(("train", P))
m, cd = m[m.series == "KXGOLD15M"], cd[cd.series == "KXGOLD15M"]
cut = m.close_ts[m.period == "train"].max()
paxg = pd.read_sql("SELECT ts, value FROM spot WHERE index_id='coinbase:PAXG-USD'",
                   sqlite3.connect(os.environ["KBOT_PAXG_DB"])).set_index("ts").value
px = paxg.set_axis(paxg.index.astype(np.int64))
px = px.reindex(np.arange(px.index.min(), px.index.max() + 60, 60)).ffill()

# the backtest's decisions (same construction as gold_proxy.py)
ts, val = [], []
for o, k0 in zip(m.open_ts.astype(np.int64), m.floor_strike):
    t = o + 60 * np.arange(15)
    ts += list(t)
    val += list(k0 * px.reindex(t).values / px.get(o, np.nan))
proxy = pd.Series(val, index=ts).dropna()
pyth, _ = e3.align({"KXGOLD15M": proxy[~proxy.index.duplicated()]}, m)
x = e3.signals(m, cd, pyth)
ref = {r.ticker: (int(r.s), "yes" if r.side > 0 else "no")
       for r in e3.taker(e3.blend(x[x.close_ts > cut].copy(), e3.fit(x[x.close_ts <= cut]))).itertuples()}

# the live rule, fed one token price per minute (the live bot polls every 2 s, so every minute has data)
grid = list(zip(px.index.astype(float), px.values))
at = {int(a): i for i, (a, _) in enumerate(grid)}
rule = GoldToken(StrategyConfig(name="gold_token", min_net_edge=0.0, max_spread=0.05, min_top_size=0))
mt, ct = m[m.period == P].set_index("ticker"), cd[cd.period == P].set_index(["ticker", "k"])
live, net, clus = {}, [], []
for tk, r in mt.iterrows():
    info = MarketInfo(tk, r.series, "E", r.open_ts, r.close_ts, "active", r.floor_strike, "greater_or_equal", "")
    for s in CHECKPOINTS:
        if (tk, 15 - s // 60) not in ct.index or int(r.close_ts - s) not in at:
            continue
        c = ct.loc[(tk, 15 - s // 60)]
        if not (c.bc > 0 and c.ac < 1):
            continue
        b = OrderBook(tk, updated_at=float(c.end_ts))
        b.yes_bids, b.no_bids = {round(c.bc, 2): 100}, {round(1 - c.ac, 2): 100}
        i = at[int(r.close_ts - s)]
        out = rule.evaluate(Snapshot(now=float(c.end_ts), market=info, book=b, spot_points=grid[max(0, i - 80):i + 1]))
        if isinstance(out, Candidate):
            live[tk] = (s, out.outcome_side)
            price = (c.ac if out.outcome_side == "yes" else 1 - c.bc) + 0.01
            win = r.y if out.outcome_side == "yes" else 1 - r.y
            net.append(win - price - fee1(min(max(price, 0.01), 0.99)))
            clus.append(r.close_ts)
            break
both = set(ref) & set(live)
print(f"{P}: backtest signals {len(ref)}, live-rule signals {len(live)}, same checkpoint and side "
      f"{sum(ref[k] == live[k] for k in both)}")
if net:
    print(f"live rule: {len(net)} trades, {np.mean(net) * 100:+.2f}c per contract, 95% CI {boot_ci(np.array(net), np.array(clus))}")

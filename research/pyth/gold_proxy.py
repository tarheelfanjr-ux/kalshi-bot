"""No Pyth: does an outside gold price (Coinbase PAXG-USD, a gold-backed token) add information beyond
the Kalshi gold mid? Runs E3's code with Pyth replaced by PAXG. By default it explores gold train only:
fit on its first half, report its second. A proxy: a pass supports E3, a fail does not rule Pyth out
(PAXG trades thinly and may lag spot gold).

    KBOT_CAND_DB=... KBOT_FULL_DB=... KBOT_PAXG_DB=... python gold_proxy.py            # train halves
    python gold_proxy.py valid | test     # spec.json gold_proxy_holdout: fit on all of train, read once
"""
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

import run_e3 as e3
from common import panel

P = sys.argv[1] if len(sys.argv) > 1 else "explore"
m, cd = panel(("train",) if P == "explore" else ("train", P))
m, cd = m[m.series == "KXGOLD15M"], cd[cd.series == "KXGOLD15M"]
cut = m.close_ts.median() if P == "explore" else m.close_ts[m.period == "train"].max()
paxg = pd.read_sql("SELECT ts, value FROM spot WHERE index_id='coinbase:PAXG-USD'",
                   sqlite3.connect(os.environ["KBOT_PAXG_DB"])).set_index("ts").value
paxg = paxg.set_axis(paxg.index.astype(np.int64))
px = paxg.reindex(np.arange(paxg.index.min(), paxg.index.max() + 60, 60)).ffill()

# Rescale PAXG to each window's strike so its premium over spot gold cancels: strike * PAXG(t) / PAXG(open).
# shortcut: sigma then also sees the PAXG-vs-Pyth gap at each window edge (a few % of variance); fine for a proxy.
ts, val = [], []
for o, k0 in zip(m.open_ts.astype(np.int64), m.floor_strike):
    t = o + 60 * np.arange(15)
    ts += list(t)
    val += list(k0 * px.reindex(t).values / px.get(o, np.nan))
proxy = pd.Series(val, index=ts).dropna()
proxy = proxy[~proxy.index.duplicated()]

pyth, _ = e3.align({"KXGOLD15M": proxy}, m)
x = e3.signals(m, cd, pyth)
w = e3.fit(x[x.close_ts <= cut])
ev = e3.blend(x[x.close_ts > cut].copy(), w)
evs = e3.blend(e3.signals(m[m.close_ts > cut], cd[cd.close_ts > cut], pyth, lag=60), w)
t = e3.taker(ev)
rep = {"label": ("EXPLORATION, PROXY (Coinbase PAXG-USD, not Pyth), gold train half only" if P == "explore" else
                 f"HISTORICAL TEST, PROXY (Coinbase PAXG-USD, not Pyth), gold {P}, rules frozen in spec.json"),
       "period": P,
       "fit_markets": int((m.close_ts <= cut).sum()), "eval_markets": int((m.close_ts > cut).sum()),
       "eval_span_utc": [str(pd.Timestamp(m.open_ts[m.close_ts > cut].min(), unit="s")),
                         str(pd.Timestamp(m.close_ts.max(), unit="s"))],
       "usable_checkpoints": round(float(ev.ok.mean()), 3), "fits": w, "information": e3.info(ev),
       "taker": e3.rec(t), "taker_by_checkpoint": e3.rec(t, "s"),
       "stress": {"latency_60s": e3.rec(e3.taker(ev, book="next")), "stale_proxy_60s": e3.rec(e3.taker(evs)),
                  "slippage_0c": e3.rec(e3.taker(ev, slip=0.0))}}
json.dump(rep, open(f"gold-proxy-results-{P}.json", "w"), indent=1, default=float)
print(json.dumps(rep, indent=1, default=float))

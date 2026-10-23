"""Outcome-free data inventory: markets, dates, strike precision, quotes, spreads, volume, open interest."""
import sqlite3

import numpy as np
import pandas as pd

from common import CAND_DB, markets

m = markets()
cd = pd.read_sql("SELECT * FROM candles", sqlite3.connect(CAND_DB)).merge(m[["ticker", "series", "open_ts"]], on="ticker")
cd["k"] = ((cd.end_ts - cd.open_ts) // 60).astype(int)
ins = cd[(cd.k >= 1) & (cd.k <= 14)].copy()
ins["two"] = (ins.yes_bid > 0) & (ins.yes_ask < 1) & (ins.yes_ask > ins.yes_bid)
ins["spr"] = np.where(ins.two, ins.yes_ask - ins.yes_bid, np.nan)
rows = []
for s, g in m.groupby("series"):
    c = ins[ins.series == s]
    vol = c.groupby("ticker").volume.sum()
    days = (g.close_ts.max() - g.open_ts.min()) / 86400
    rows.append(dict(series=s, markets=len(g), first=pd.Timestamp(g.open_ts.min(), unit="s").strftime("%Y-%m-%d"),
                     last=pd.Timestamp(g.close_ts.max(), unit="s").strftime("%Y-%m-%d %H:%M"),
                     per_day=round(len(g) / days, 1),
                     strike_decimals=int(g.floor_strike.dropna().map(lambda x: len(("%.8f" % x).rstrip("0").split(".")[1])).max()),
                     two_sided=round(c.two.mean(), 3), spread_median=c.spr.median(), spread_1c_share=round((c.spr <= 0.0101).mean(), 3),
                     volume_median=round(vol.median(), 1), volume_mean=round(vol.mean(), 1),
                     minutes_with_volume=round((c.volume > 0).mean(), 3),
                     oi_at_close_median=round(cd[(cd.series == s) & (cd.k == 15)].open_interest.median(), 1)))
inv = pd.DataFrame(rows).set_index("series")
pd.set_option("display.width", 250)
print(inv.to_string())
inv.to_json("inventory.json", orient="index", indent=1)

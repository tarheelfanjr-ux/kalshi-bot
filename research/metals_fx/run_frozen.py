"""Run every strategy frozen in spec.json on one period (train, valid, test, or e.g. valid+test).

    python run_frozen.py test > test.txt     # writes results-test.json next to the working dir
"""
import json
import sys

import numpy as np
import pandas as pd

from common import (both_entries, fav_entries, fee1, fills, improve_entries, maker_fee, panel, settle,
                    summarize)

P = sys.argv[1]
m, cd = panel(tuple(P.split("+")))
nxt = cd[["ticker", "k", "bc", "ac", "two"]].copy()
nxt["k"] -= 1
cdn = cd.merge(nxt.rename(columns={"bc": "nbc", "ac": "nac", "two": "ntwo"}), on=["ticker", "k"], how="left")


def taker(rows, side, stress=False):
    """Buy at the ask (YES) or 1-bid (NO) of minute k0 + 1c slippage; stress uses the next minute's book."""
    a, b = (rows.nac, rows.nbc) if stress else (rows.ac, rows.bc)
    t = pd.DataFrame(dict(ticker=rows.ticker.values, price=np.where(side > 0, a, 1 - b) + 0.01, side=side))
    t = t.merge(m[["ticker", "series", "grp", "close_ts", "y"]], on="ticker")
    t = t[t.price.notna() & (t.price < 1)]
    t["win"] = np.where(t.side > 0, t.y, 1 - t.y)
    t["gross"] = t.win - t.price
    t["fee"] = fee1(t.price.clip(0.01, 0.99).values)
    t["net"] = t.gross - t.fee
    return t


def taker_pair(rows, side):
    ok = (rows.ntwo == True).values  # noqa: E712 (NaN-safe)
    return taker(rows, side), taker(rows[ok], side[ok], stress=True)


def fav(rows):
    return rows[((rows.mid >= .6) & (rows.mid <= .9)) | ((1 - rows.mid >= .6) & (1 - rows.mid <= .9))]


out = {}
k1 = cdn[(cdn.k == 1) & cdn.two]
out["T1"] = taker_pair(k1, np.random.default_rng(7).choice([-1, 1], len(k1)))
out["T2"] = taker_pair(k1, np.ones(len(k1)))
k5 = fav(cdn[(cdn.k == 5) & cdn.two])
out["T3"] = taker_pair(k5, np.where(k5.mid > 0.5, 1, -1))

for name, e, front in [("M2", both_entries(cd, 1), False), ("M1", fav_entries(cd, 5), False),
                       ("M3", improve_entries(cd, 3), True), ("M3b", improve_entries(cd, 5), True),
                       ("M3c", improve_entries(cd, 8), True)]:
    q = settle(fills(cd, e, at_price=front), m)
    f = q[q.filled].copy()
    stress = {"maker_fee": f.assign(fee=maker_fee(f.price.values), net=f.gross - maker_fee(f.price.values)),
              "fills_from_k0+2": f[f.fill_k >= f.k0 + 2]}
    if front:
        s = settle(fills(cd, e), m)
        stress["strictly_through"] = s[s.filled]
    out[name] = (f, stress, q)


def rec(t, by):
    return summarize(t, by).round(4).reset_index().to_dict("records") if len(t) else []


rep = {"period": P, "markets": int(len(m)),
       "span_utc": [str(pd.Timestamp(m.open_ts.min(), unit="s")), str(pd.Timestamp(m.close_ts.max(), unit="s"))],
       "strategies": {}}
for name, (base, stress, *q) in out.items():
    r = {"by_series": rec(base, "series"), "by_group": rec(base, "grp")}
    if name.startswith("T"):
        r["stress_next_minute_by_group"] = rec(stress, "grp")
    else:
        r["stress_by_group"] = {k: rec(x, "grp") for k, x in stress.items()}
        r["quotes_by_group"] = q[0].groupby("grp").agg(quotes=("ticker", "size"), fills=("filled", "sum")) \
                                   .reset_index().to_dict("records")
    rep["strategies"][name] = r
json.dump(rep, open(f"results-{P}.json", "w"), indent=1, default=float)

pd.set_option("display.width", 250)
for name, v in out.items():
    print("==", name, "(by group)")
    print(summarize(v[0], "grp").round(4).to_string())
pm = out["M3"][0]
print("== PRIMARY M3, metals, by series")
print(summarize(pm[pm.grp == "metal"], "series").round(4).to_string())
for k, x in out["M3"][1].items():
    print("== PRIMARY stress:", k)
    print(summarize(x[x.grp == "metal"]).round(4).to_string())

"""Everything read on the train (exploration) half before the spec was frozen. Train only.

Prints: outcome minus mid by minute and price, tie rates, window-to-window direction, quote
momentum, cross-market lead, and the taker and resting-bid grids that picked the frozen rules.
"""
import numpy as np
import pandas as pd

from common import both_entries, boot_ci, fav_entries, fee1, fills, improve_entries, panel, settle

pd.set_option("display.width", 250)
m, cd = panel(("train",))
c = cd[cd.two & (cd.k <= 14)].copy()

print("== outcome minus mid, by minutes since open and mid")
c["bin"] = pd.cut(c.mid, [0, .1, .2, .3, .4, .45, .55, .6, .7, .8, .9, 1])
for g in ["metal", "fx", "brent", "yield"]:
    x = c[c.grp == g]
    t = x.groupby([pd.cut(x.k, [0, 2, 5, 9, 12, 14]), "bin"], observed=True).apply(lambda d: d.y.mean() - d.mid.mean())
    print(g)
    print(t.unstack(0).round(3).to_string())

print("== ties (resolve YES) by series and UTC hour (FX)")
mm = m.sort_values(["series", "close_ts"]).copy()
mm["hr"] = pd.to_datetime(mm.open_ts, unit="s").dt.hour
print(mm.groupby("series").tie.mean().round(4).to_string())
print(mm[mm.grp == "fx"].groupby("hr").tie.mean().round(3).to_dict())

print("== P(up | previous window up / down), ties excluded")
mm["ret"] = np.sign(mm.ev - mm.floor_strike)
g = mm.groupby("series")
mm["prev"] = g.ret.shift()
x = mm[(mm.open_ts == g.close_ts.shift()) & (mm.ret != 0) & (mm.prev != 0)]
print(x.groupby(["series", "prev"]).ret.apply(lambda r: (r > 0).mean()).unstack().round(3).to_string())

print("== quote momentum: corr(outcome - mid, last minute's mid change)")
c["dmid"] = c.mid - c.groupby("ticker").mid.shift()
for g in ["metal", "fx"]:
    for k in [3, 6, 9, 12]:
        z = c[(c.grp == g) & (c.k == k)].dropna(subset=["dmid"])
        print(g, k, len(z), round((z.y - z.mid).corr(z.dmid), 3))

print("== cross-market: corr(outcome - mid, peers' mids in this market's direction)")
SGN = {"KXEURUSD15M": 1, "KXGBPUSD15M": 1, "KXAUDUSD15M": 1, "KXUSDJPY15M": -1, "KXUSDCAD15M": -1,
       "KXPLATINUM15M": 1, "KXPALLADIUM15M": 1, "KXCOPPER15M": 1}
for peers in [[s for s in SGN if SGN[s] and "USD" in s], ["KXPLATINUM15M", "KXPALLADIUM15M", "KXCOPPER15M"]]:
    x = c[c.series.isin(peers)].copy()
    x["sm"] = x.series.map(SGN) * (x.mid - 0.5)
    piv = x.pivot_table(index=["close_ts", "k"], columns="series", values="sm")
    for s in peers:
        t = x[x.series == s].set_index(["close_ts", "k"]).join(piv[[p for p in peers if p != s]].mean(axis=1).rename("peer"))
        for k in [3, 6, 9, 12]:
            z = t[t.index.get_level_values("k") == k].dropna(subset=["peer"])
            print(s, k, len(z), round((z.y - z.mid).corr(SGN[s] * z.peer), 3))

print("== taker grid: buy the favorite at the ask + 1c, cent-rounded 1-contract fee")
for g in ["metal", "fx"]:
    for k in [2, 5, 8, 10, 12]:
        for lo, hi in [(0.55, 0.7), (0.7, 0.85), (0.85, 0.95)]:
            x = c[(c.grp == g) & (c.k == k)]
            yes, no = x[(x.mid >= lo) & (x.mid < hi)], x[(1 - x.mid >= lo) & (1 - x.mid < hi)]
            px = np.r_[yes.ac + 0.01, 1 - no.bc + 0.01]
            win = np.r_[yes.y, 1 - no.y]
            ok = px < 1
            if ok.sum() >= 20:
                print(g, k, f"{lo}-{hi}", int(ok.sum()), round((win - px - fee1(np.clip(px, .01, .99)))[ok].mean(), 4))

print("== resting-bid grid (per fill, zero maker fee)")
for name, e, front in ([(f"join favorite k0={k}", fav_entries(cd, k), False) for k in [1, 3, 5, 8, 10]]
                       + [(f"join both sides k0={k}", both_entries(cd, k), False) for k in [1, 5]]
                       + [(f"improve favorite k0={k}", improve_entries(cd, k), True) for k in [3, 5, 8]]):
    f = settle(fills(cd, e, at_price=front), m)
    f = f[f.filled]
    print(name)
    print(f.groupby("grp").apply(lambda d: pd.Series(dict(fills=len(d), per_fill=round(d.net.mean(), 4),
                                                          ci=boot_ci(d.net.values, d.close_ts.values)))).to_string())

"""E3: does Pyth, the settlement source, add information beyond the Kalshi mid, enough to pay a taker?

Every rule is frozen in spec.json; periods are fixed in coverage.json. Inputs, as in metals_fx:
  KBOT_CAND_DB   kbot database with the four series' markets and Pyth bars in `spot` (kbot collect-pyth)
  KBOT_FULL_DB   full minute candles from ../metals_fx/fetch_full.py

    python run_e3.py train      # fits on train, reports train
    python run_e3.py valid      # then test, once; then testB once enough prospective data exists
    python run_e3.py control    # plumbing check on train with synthetic Pyth bars, no real Pyth needed
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
os.environ.setdefault("KBOT_COVERAGE", str(HERE / "coverage.json"))
sys.path[:0] = [str(HERE.parent / "metals_fx"), str(HERE.parents[1])]
import common  # noqa: E402
from common import boot_ci, fee1, fills, maker_fee, panel, settle, summarize  # noqa: E402

from kbot.audit import _logloss, _sigmoid, fit_logit  # noqa: E402
from kbot.model import prob_settle_yes_print  # noqa: E402

SPEC = json.load(open(HERE / "spec.json"))
COV = json.load(open(os.environ["KBOT_COVERAGE"]))
SERIES = SPEC["series"]
K = {15 - s // 60: s for s in SPEC["checkpoints_s"]}       # minute k whose candle ends at T -> seconds left
TESTB_START = pd.Timestamp("2026-10-24T00:00:00Z").timestamp()
common.GROUP.update({s: s[2:-3].lower() for s in SERIES})


def markets():
    """Settled markets of the four series with fixed period boundaries (coverage.json)."""
    m = _all_markets()
    m = m[m.series.isin(SERIES)].copy()
    ts = {p: m.series.map(lambda s: pd.Timestamp(COV[s][p]).timestamp())
          for p in ("exploration_end_utc", "valid_end_utc", "test_end_utc")}
    m["period"] = np.select([m.close_ts <= ts["exploration_end_utc"], m.close_ts <= ts["valid_end_utc"],
                             m.close_ts <= ts["test_end_utc"], m.open_ts >= TESTB_START],
                            ["train", "valid", "test", "testB"], "gap")
    return m


_all_markets, common.markets = common.markets, markets      # panel() reads periods through markets()


# --- Pyth -----------------------------------------------------------------------------------

def raw_pyth():
    con = sqlite3.connect(common.CAND_DB)
    return {s: pd.read_sql("SELECT ts, value FROM spot WHERE index_id=?", con, params=(f"pyth:{sym}",))
                 .set_index("ts").value for s, sym in SPEC["pyth_symbols"].items()}


def align(raw, m):
    """Shift each series' bars so the value at T is the last print at or before T. The shift is the
    offset at which Pyth at open equals floor_strike (known before trading, so outcome-free)."""
    out, report = {}, {}
    for s, p in raw.items():
        p = p.set_axis(p.index.astype(np.int64))
        g = m[(m.series == s) & m.floor_strike.notna()]
        hit = {}
        for off in (0, -60, 60):
            v = p.reindex(g.open_ts.astype(np.int64) + off).values
            ok = ~np.isnan(v)
            hit[off] = (np.isclose(v[ok], g.floor_strike.values[ok], rtol=1e-6, atol=0).mean(), int(ok.sum()))
        off, (share, n) = max(hit.items(), key=lambda kv: kv[1][0])
        report[s] = {"offset_s": off, "match": round(share, 4), "markets_with_bar": n}
        if n < 100 or share < 0.99:
            raise SystemExit(f"{s}: Pyth bars do not line up with the strike chain {hit}; refusing to run")
        out[s] = p.set_axis(p.index - off)
    return out, report


def features(p):
    """Per minute: last price, sigma per second over the last hour, real bars in that hour, stale flag."""
    grid = np.arange(p.index.min(), p.index.max() + 60, 60)
    real = p.reindex(grid)
    px = real.ffill()
    r2 = np.log(px).diff() ** 2
    return pd.DataFrame(dict(px=px, sig=np.sqrt(r2.rolling(60).sum() / 3600), n=real.notna().rolling(60).sum(),
                             stale=px.rolling(11).max() == px.rolling(11).min()), index=grid)


def signals(m, cd, pyth, lag=0):
    """One row per market and checkpoint with the book at T, the next minute's book, and q."""
    nxt = cd[["ticker", "k", "bc", "ac"]].assign(k=cd.k - 1).rename(columns={"bc": "nbc", "ac": "nac"})
    x = cd[cd.k.isin(K)].merge(nxt, on=["ticker", "k"], how="left")
    x = x.merge(m[["ticker", "floor_strike"]], on="ticker")
    x["s"] = x.k.map(K)
    parts = []
    for s, g in x.groupby("series"):
        f = features(pyth[s]).reindex(g.end_ts - lag)
        parts.append(g.assign(px=f.px.values, sig=f.sig.values, n=f.n.values, stale=f.stale.values))
    x = pd.concat(parts)
    tick = x.series.map(SPEC["tick"])
    x["ok"] = (x.two & (x.ac - x.bc <= 0.05 + 1e-9) & x.px.notna() & (x.n >= 30) & (x.sig > 0)
               & (x.stale != True))  # noqa: E712 (NaN-safe)
    x["q"] = [prob_settle_yes_print(a, b, s + lag, c, t) if ok else np.nan
              for a, b, s, c, t, ok in zip(x.px, x.floor_strike, x.s, x.sig, tick, x.ok)]
    return x.sort_values(["ticker", "k"]).reset_index(drop=True)


# --- blend and tests ------------------------------------------------------------------------

def lg(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


def fit(train):
    """Per checkpoint on train: M0 on logit(mid), M1 adds logit(q) - logit(mid)."""
    w = {}
    for s, g in train[train.ok].groupby("s"):
        a, d = lg(g.mid.values), lg(g.q.values) - lg(g.mid.values)
        w[int(s)] = (fit_logit([[v] for v in a], g.y.tolist()), fit_logit(list(zip(a, d)), g.y.tolist()))
    return w


def blend(x, w):
    a, d = lg(x.mid.values), lg(np.nan_to_num(x.q.values, nan=0.5)) - lg(x.mid.values)
    w0 = np.array([w[s][0] for s in x.s])
    w1 = np.array([w[s][1] for s in x.s])
    x["p0"] = [_sigmoid(z) for z in w0[:, 0] + w0[:, 1] * a]
    x["p1"] = [_sigmoid(z) for z in w1[:, 0] + w1[:, 1] * a + w1[:, 2] * d]
    return x


def info(x):
    z = x[x.ok].copy()
    z["gain"] = [_logloss(a, y) - _logloss(b, y) for a, b, y in zip(z.p0, z.p1, z.y)]
    one = lambda d: dict(n=len(d), gain=round(d.gain.mean(), 5), ci=boot_ci(d.gain.values, d.close_ts.values))  # noqa: E731
    return {"pooled": one(z), "by_checkpoint": {int(s): one(d) for s, d in z.groupby("s")}}


def taker(x, slip=0.01, book="now", margin=0.01):
    """First checkpoint per market whose edge after slippage and fee beats the margin; hold to settlement."""
    py, pn = x.ac + slip, 1 - x.bc + slip
    ey = x.p1 - py - fee1(py.clip(0.01, 0.99).values)
    en = (1 - x.p1) - pn - fee1(pn.clip(0.01, 0.99).values)
    x = x.assign(side=np.where(ey >= en, 1, -1))
    x = x[x.ok & (np.maximum(ey, en) > margin)].groupby("ticker").head(1)
    a, b = (x.nac, x.nbc) if book == "next" else (x.ac, x.bc)
    t = x.assign(price=np.where(x.side > 0, a, 1 - b) + slip)
    t = t[t.price.notna() & (t.price < 1)]
    t["win"] = np.where(t.side > 0, t.y, 1 - t.y)
    t["gross"] = t.win - t.price
    t["fee"] = fee1(t.price.clip(0.01, 0.99).values)
    t["net"] = t.gross - t.fee
    return t


def maker(x, cd, m, margin=0.02):
    ey, en = x.p1 - x.bc, (1 - x.p1) - (1 - x.ac)
    x = x.assign(side=np.where(ey >= en, 1, -1))
    x = x[x.ok & (np.maximum(ey, en) > margin)].groupby("ticker").head(1)
    e = pd.DataFrame(dict(ticker=x.ticker, k0=x.k, side=x.side, price=np.where(x.side > 0, x.bc, 1 - x.ac).round(2)))
    q = settle(fills(cd, e), m)
    f = q[q.filled].copy()
    mf = maker_fee(f.price.values)
    return q, f, {"maker_fee": f.assign(fee=mf, net=f.gross - mf), "fills_from_k0+2": f[f.fill_k >= f.k0 + 2]}


def rec(t, by=None):
    if not len(t):
        return []
    r = summarize(t, by).round(4)
    return r.to_dict() if by is None else r.reset_index().to_dict("records")


def run(P, raw):
    """Fit on train, report period(s) P. Train results are in-sample."""
    ps = P.split("+")
    m_all, cd_all = panel(tuple({"train", *ps}))
    pyth, alignment = align(raw, m_all)
    x_all = signals(m_all, cd_all, pyth)
    w = fit(x_all[x_all.period == "train"])
    m, cd = m_all[m_all.period.isin(ps)], cd_all[cd_all.period.isin(ps)]
    x = blend(x_all[x_all.period.isin(ps)].copy(), w)
    xs = blend(signals(m, cd, pyth, lag=60), w)
    base = taker(x)
    small = base[base.price + base.fee <= 0.25]
    q, f, mstress = maker(x, cd, m)
    rep = {"experiment": "E3", "period": P, "label": "HISTORICAL TEST on saved Kalshi data and Pyth bars",
           "alignment": alignment, "fits": {s: {"M0": a, "M1": b} for s, (a, b) in w.items()},
           "markets": int(len(m)), "coverage": {"market_checkpoints": int(len(x)), "usable": int(x.ok.sum()),
                                                "by_series": x.groupby("series").ok.mean().round(3).to_dict()},
           "information": info(x),
           "taker": {"pooled": rec(base), "by_series": rec(base, "series"), "by_checkpoint": rec(base, "s"),
                     "signal_rate": round(base.ticker.nunique() / max(len(m), 1), 4),
                     "stress": {"latency_60s": rec(taker(x, book="next")), "stale_pyth_60s": rec(taker(xs)),
                                "slippage_2c": rec(taker(x, slip=0.02)),
                                "slippage_0c": rec(taker(x, slip=0.0)), "margin_0.02": rec(taker(x, margin=0.02))},
                     "costing_at_most_25c": rec(small)},
           "maker": {"quotes": int(len(q)), "fills": int(len(f)), "pooled": rec(f), "by_series": rec(f, "series"),
                     "stress": {k: rec(v) for k, v in mstress.items()}}}
    return rep


def synthetic(m, bridge, seed=3):
    """Control bars: a random walk inside each window from floor_strike. bridge=True pins it to the
    settlement value at close (it sees the future, so the tests must light up); False leaves it free."""
    rng, out = np.random.default_rng(seed), {}
    for s, g in m[m.floor_strike.notna() & m.ev.notna()].groupby("series"):
        vol = np.nanstd(np.log(g.ev / g.floor_strike)) / np.sqrt(15)
        ts, val = [], []
        for o, c, k0, ev in zip(g.open_ts, g.close_ts, g.floor_strike, g.ev):
            w = np.r_[0, np.cumsum(rng.normal(0, vol, 15))]
            if bridge:
                w = w - np.arange(16) / 15 * (w[-1] - np.log(ev / k0))
            ts += list(o + 60 * np.arange(15))
            val += list(k0 * np.exp(w[:15]))
        out[s] = pd.Series(val, index=ts).groupby(level=0).first()
    return out


if __name__ == "__main__":
    P = sys.argv[1]
    if P == "control":
        m, _ = panel(("train",))
        rep = {}
        for name, br in (("free", False), ("bridge", True)):
            r = run("train", synthetic(m, br))
            rep[name] = {"information": r["information"]["pooled"], "taker": r["taker"]["pooled"],
                         "alignment": r["alignment"]}
            print(name, json.dumps(rep[name], default=float))
        free, bridge = rep["free"], rep["bridge"]
        assert bridge["information"]["gain"] > 0.05 and bridge["taker"]["per_contract"] > 0.05, "bridge must light up"
        assert abs(free["information"]["gain"]) < 0.01 and (not free["taker"] or free["taker"]["per_contract"] < 0.01), \
            "free walk must not"
        print("ok: synthetic controls behave (CONTROL ONLY, not a Pyth result)")
    else:
        rep = run(P, raw_pyth())
        json.dump(rep, open(f"e3-results-{P}.json", "w"), indent=1, default=float)
        print(json.dumps({k: rep[k] for k in ("period", "markets", "coverage", "information")}, default=float))
        print("taker", json.dumps(rep["taker"]["pooled"], default=float))

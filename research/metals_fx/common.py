"""Shared loaders, fill rules and metrics for the metals/FX/Brent/yield edge test (2026-10-23).

Paths come from the environment so the scripts run against a copy of the saved data:
  KBOT_CAND_DB      candidates-history.sqlite (markets; read-only, work on a copy)
  KBOT_FULL_DB      full minute candles from fetch_full.py (trade price low/high per minute)
  KBOT_COVERAGE     candidates-coverage-2026-10-23.json (exploration_end_utc per series)
"""
import json
import os
import sqlite3

import numpy as np
import pandas as pd

CAND_DB = os.environ.get("KBOT_CAND_DB", "data/candidates-history.sqlite")
FULL_DB = os.environ.get("KBOT_FULL_DB", "data/candidates-full-candles.sqlite")
COVERAGE = os.environ.get("KBOT_COVERAGE", "reports/candidates-coverage-2026-10-23.json")

GROUP = {"KXCOPPER15M": "metal", "KXPLATINUM15M": "metal", "KXPALLADIUM15M": "metal",
         "KXEURUSD15M": "fx", "KXGBPUSD15M": "fx", "KXUSDJPY15M": "fx", "KXUSDCAD15M": "fx",
         "KXAUDUSD15M": "fx", "KXBRENT15M": "brent", "KX2YRRATE15M": "yield", "KX5YRRATE15M": "yield",
         "KX10YRRATE15M": "yield", "KX30YRRATE15M": "yield"}


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return np.nan


def markets():
    """Every settled market with its period. Train = the exploration half named in the coverage
    report; the rest is split in half by count per series: valid (earlier), test (later)."""
    cov = json.load(open(COVERAGE))
    m = pd.read_sql("SELECT ticker, series, open_ts, close_ts, floor_strike, result, data FROM markets",
                    sqlite3.connect(CAND_DB))
    d = m.pop("data").map(json.loads)
    m["ev"] = d.map(lambda x: _num(x.get("expiration_value")))
    m = m.sort_values(["series", "close_ts"]).reset_index(drop=True)
    m["period"] = "train"
    for s, g in m.groupby("series"):
        rest = g[g.close_ts > pd.Timestamp(cov[s]["exploration_end_utc"]).timestamp()].index
        m.loc[rest[:len(rest) // 2], "period"] = "valid"
        m.loc[rest[len(rest) // 2:], "period"] = "test"
    return m


def fee1(p, n=1):
    """Taker fee per contract for one order of n contracts: ceil to the cent of 0.07*n*p*(1-p)."""
    return np.ceil(np.round(0.07 * n * p * (1 - p) * 100, 6) - 1e-9) / 100 / n


def maker_fee(p):
    """Stress only: a 0.0175 maker fee per 1-contract order, as on quadratic_with_maker_fees series."""
    return np.ceil(np.round(0.0175 * p * (1 - p) * 100, 6) - 1e-9) / 100


def _f(d, k):
    v = (d or {}).get(k + "_dollars", (d or {}).get(k))
    return float(v) if v not in (None, "") else np.nan


def full_candles(tickers):
    """Minute candles: bid/ask OHLC (bo..bc, ao..ac) and trade price low/high/close (pl, ph, pc)."""
    tk, rows = set(tickers), []
    for t, e, j in sqlite3.connect(FULL_DB).execute("SELECT ticker, end_ts, j FROM raw"):
        if t in tk:
            d = json.loads(j)
            b, a, p = d.get("yes_bid"), d.get("yes_ask"), d.get("price")
            rows.append((t, e, _f(b, "open"), _f(b, "high"), _f(b, "low"), _f(b, "close"), _f(a, "open"),
                         _f(a, "high"), _f(a, "low"), _f(a, "close"), _f(p, "low"), _f(p, "high"),
                         _f(p, "close"), float(d.get("volume_fp") or 0)))
    return pd.DataFrame(rows, columns="ticker end_ts bo bh bl bc ao ah al ac pl ph pc vol".split())


def panel(periods):
    """Markets in `periods` with outcome columns, and their candles indexed by minute k (1..15)."""
    m = markets()
    m = m[m.period.isin(periods) & m.floor_strike.notna()].copy()
    m["y"] = (m.result == "yes").astype(int)
    m["tie"] = np.isclose(m.ev, m.floor_strike, rtol=0, atol=1e-9).astype(int)
    m["grp"] = m.series.map(GROUP)
    cd = full_candles(m.ticker).merge(m[["ticker", "series", "grp", "open_ts", "close_ts", "y", "tie", "period"]],
                                      on="ticker")
    cd["k"] = ((cd.end_ts - cd.open_ts) // 60).astype(int)
    cd = cd[(cd.k >= 1) & (cd.k <= 15)].sort_values(["ticker", "k"]).reset_index(drop=True)
    cd["two"] = (cd.bc > 0) & (cd.ac < 1) & (cd.ac > cd.bc)
    cd["mid"] = (cd.ac + cd.bc) / 2
    return m, cd


# --- resting bids ---------------------------------------------------------------------------
# entries: ticker, k0 (minute whose closing book we quote off), side (+1 YES bid, -1 NO bid), price.

def _frame(yes, no, k0, yes_px, no_px):
    e = pd.concat([pd.DataFrame(dict(ticker=yes.ticker, k0=k0, side=1, price=yes_px)),
                   pd.DataFrame(dict(ticker=no.ticker, k0=k0, side=-1, price=no_px))])
    e["price"] = e.price.round(2)
    return e


def fav_entries(cd, k0, lo=0.60, hi=0.90):
    """Join the favorite's best bid when the favorite's mid is in [lo, hi]."""
    x = cd[(cd.k == k0) & cd.two]
    yes, no = x[(x.mid >= lo) & (x.mid <= hi)], x[(1 - x.mid >= lo) & (1 - x.mid <= hi)]
    e = _frame(yes, no, k0, yes.bc, 1 - no.ac)
    return e[(e.price > 0) & (e.price < 1)].reset_index(drop=True)


def improve_entries(cd, k0, lo=0.60, hi=0.90):
    """Bid 1c above the favorite's best bid when the spread is at least 2c (front of the book)."""
    x = cd[(cd.k == k0) & cd.two & ((cd.ac - cd.bc) >= 0.0199)]
    yes, no = x[(x.mid >= lo) & (x.mid <= hi)], x[(1 - x.mid >= lo) & (1 - x.mid <= hi)]
    e = _frame(yes, no, k0, yes.bc + 0.01, 1 - no.ac + 0.01)
    return e[(e.price > 0) & (e.price < 1)].reset_index(drop=True)


def both_entries(cd, k0):
    """Join the best bid on both sides (E2's rule, once per market), bids 0.05 to 0.95."""
    x = cd[(cd.k == k0) & cd.two]
    e = _frame(x, x, k0, x.bc, 1 - x.ac)
    return e[(e.price >= 0.05) & (e.price <= 0.95)].reset_index(drop=True)


def fills(cd, entries, at_price=False, kend=14):
    """First later minute (k0, kend] whose trades reach our bid. A YES bid at b fills on a trade
    below b (at or below b if at_price, i.e. we are alone at the front); a NO bid at n fills on a
    YES trade above 1-n. Quotes are cancelled after minute kend."""
    lo = cd.pivot_table(index="ticker", columns="k", values="pl", aggfunc="first")
    hi = cd.pivot_table(index="ticker", columns="k", values="ph", aggfunc="first")
    eps = 1e-9 if at_price else -1e-9
    out = []
    for r in entries.itertuples():
        src = lo if r.side > 0 else hi
        if r.ticker not in src.index:
            out.append(np.nan)
            continue
        s = src.loc[r.ticker, [k for k in range(int(r.k0) + 1, kend + 1) if k in src.columns]]
        hit = s[s < r.price + eps] if r.side > 0 else s[s > 1 - r.price - eps]
        out.append(hit.index[0] if len(hit) else np.nan)
    e = entries.copy()
    e["fill_k"] = out
    return e


def settle(e, m):
    """Hold each filled bid to settlement. Maker fee is zero on these series (fee_type quadratic)."""
    e = e.merge(m[["ticker", "series", "grp", "close_ts", "y", "tie"]], on="ticker", how="left")
    e["win"] = np.where(e.side > 0, e.y, 1 - e.y)
    e["filled"] = e.fill_k.notna()
    e["gross"] = np.where(e.filled, e.win - e.price, 0.0)
    e["fee"] = 0.0
    e["net"] = e.gross
    return e


# --- metrics --------------------------------------------------------------------------------

def boot_ci(vals, clusters, n=2000, seed=1):
    """95% CI of the mean, resampling whole settlement windows (markets closing together)."""
    df = pd.DataFrame(dict(v=vals, c=clusters)).groupby("c").v.agg(["sum", "size"])
    if len(df) < 5:
        return (np.nan, np.nan)
    idx = np.random.default_rng(seed).integers(0, len(df), (n, len(df)))
    st = df["sum"].values[idx].sum(1) / df["size"].values[idx].sum(1)
    return tuple(np.round(np.quantile(st, [0.025, 0.975]), 4))


def summarize(t, by=None):
    """Per group: trades, gross, fees, net, net per contract with CI, max drawdown, win rate."""
    def one(d):
        d = d.sort_values("close_ts")
        cum = d.net.cumsum()
        lo, hi = boot_ci(d.net.values, d.close_ts.values)
        return pd.Series(dict(trades=len(d), gross=d.gross.sum(), fees=d.fee.sum(), net=d.net.sum(),
                              per_contract=d.net.mean(), ci_lo=lo, ci_hi=hi,
                              max_dd=(cum.cummax().clip(lower=0) - cum).max(), win=(d.gross > 0).mean()))
    return one(t) if by is None else t.groupby(by).apply(one)


if __name__ == "__main__":
    # self-check of the fill rules and fees on a hand-made market
    cd = pd.DataFrame(dict(ticker="T", k=[3, 4, 5], pl=[0.70, 0.71, 0.69], ph=[0.72, 0.73, 0.74]))
    e = pd.DataFrame(dict(ticker="T", k0=3, side=[1, 1, -1, -1], price=[0.70, 0.71, 0.28, 0.27]))
    assert list(fills(cd, e).fill_k) == [5, 5, 4, 5]                    # strictly through only
    assert list(fills(cd, e, at_price=True).fill_k) == [5, 4, 4, 4]     # at or through
    assert fee1(0.5) == 0.02 and abs(fee1(0.5, 100) - 0.0175) < 1e-12 and maker_fee(0.5) == 0.01
    print("ok")

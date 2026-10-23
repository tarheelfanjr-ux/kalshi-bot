"""Pre-registered prospective experiments, judged only on data recorded after 2026-10-23.

E1  real index feed: does the CF Benchmarks RTI, read about once a second, beat the Kalshi mid?
E2  maker orders: do 1-contract resting bids at the best bid earn after adverse selection?

docs/EXPERIMENTS.md has the reasoning. Every choice that can move a result is in SPECS, and its
hash is printed on every report, so a changed rule is visibly a new experiment that needs a new
start date. Data comes from `kbot record` and `kbot backfill`, kept in its own database. Markets
that opened before PROSPECTIVE_START are never read, so the searched 90-day file and its opened
holdout cannot leak in. `status` reports coverage only. `evaluate` reads outcomes, refuses before
an experiment's end date unless forced, and counts every opening.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .audit import _logit, _logloss, _mean, _sigmoid, ci, fit_logit
from .db import DB
from .feed import UNDERLYING
from .fees import MAKER_RATE, TAKER_RATE, ceil_cent
from .model import prob_settle_yes

PROSPECTIVE_START = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M")
ROUND_DIGITS = {"KXBTC15M": 2, "KXETH15M": 2, "KXSOL15M": 4, "KXXRP15M": 4}  # custom_strike, read 2026-10-23
DAY = 86400.0
WINDOWS_PER_DAY = 96
LABEL = "PROSPECTIVE TEST (simulated fills on data recorded after 2026-10-23; no orders placed)"
ET = ZoneInfo("America/New_York")

SPECS = {
    "E1": {
        "question": "Does the CF Benchmarks RTI carry information beyond the Kalshi mid, enough to pay a taker?",
        "series": SERIES,
        "start": "first UTC midnight after the first RTI tick recorded on or after 2026-10-24",
        "fit_days": 14, "eval_days": 42, "min_coverage": 0.7,
        "checkpoints_s": [600, 300, 120, 60, 30, 10],
        "vol_lookback_s": 900, "min_ticks": 300, "max_tick_age_s": 5, "max_book_age_s": 5,
        "forecast": "kbot.model.prob_settle_yes, unchanged; blend = logit(outcome) on logit(mid), logit(q)-logit(mid)",
        "arms": {"cfbenchmarks": 0, "coinbase": 1},   # source -> position in UNDERLYING[series]
        "primary_arm": "cfbenchmarks",
        "trade": {"margin": 0.01, "latency_s": 1, "fill_window_s": 10, "fee": "exact taker", "contracts": 1},
        "pass": {"ci_level": 0.975, "min_trades": 300, "min_windows": 150,
                 "stress": [[3, 0.01, "exact"], [1, 0.0, "cent"]], "min_series_positive": 3},
    },
    "E2": {
        "question": "Do 1-contract resting bids at the best bid earn after adverse selection, with zero maker fee?",
        "series": SERIES,
        "start": "2026-10-24T00:00:00Z", "eval_days": 56, "interim_futility_day": 28, "min_coverage": 0.7,
        "quote_from_s_left": 840, "quote_until_s_left": 60, "grid_s": 60, "price_band": [0.05, 0.95],
        "latency_s": 1, "max_book_age_s": 5, "contracts": 1, "fill": "trade-through", "fee": "zero (quadratic)",
        "pass": {"ci_level": 0.975, "min_fills": 2000, "min_windows": 1000,
                 "stress": ["maker_exact fee", "5 s latency"], "min_series_positive": 3},
    },
}


def spec_hash(name: str) -> str:
    return hashlib.sha256(json.dumps(SPECS[name], sort_keys=True).encode()).hexdigest()[:12]


# --- data -----------------------------------------------------------------------------

def markets(db: DB, start: float, end: float, floor: float = PROSPECTIVE_START) -> list[dict]:
    """Settled experiment markets opening in [start, end), never before `floor` (PROSPECTIVE_START
    unless a labelled historical check asks otherwise), with the previous window's move (known at
    open) for volatility strata."""
    rows = db.q(f"SELECT ticker, series, open_ts, close_ts, floor_strike, result, data FROM markets "
                f"WHERE result IN ('yes','no') AND open_ts >= ? AND open_ts < ? "
                f"AND series IN ({','.join('?' * len(SERIES))}) ORDER BY series, close_ts",
                (max(start, floor), end, *SERIES))
    out, prev = [], {}
    for r in rows:
        m = dict(r)
        m["y"] = 1 if m["result"] == "yes" else 0
        p = prev.get(m["series"])
        m["prev_move"] = (abs(math.log(m["floor_strike"] / p["floor_strike"]))
                          if p and m["floor_strike"] and p["floor_strike"] and m["open_ts"] - p["open_ts"] == 900
                          else None)
        prev[m["series"]] = m
        out.append(m)
    return out


def _candles(db, ticker):
    return [tuple(r) for r in db.q("SELECT end_ts, yes_bid, yes_ask FROM candles WHERE ticker=? ORDER BY end_ts",
                                   (ticker,))]


def _trades(db, ticker):
    return [tuple(r) for r in db.q("SELECT ts, taker_side, yes_price, count FROM trades WHERE ticker=? ORDER BY ts",
                                   (ticker,))]


def _snaps(db, ticker):
    return [tuple(r) for r in db.q("SELECT ts, yes_bid, yes_ask, yes_bid_size, yes_ask_size FROM snapshots "
                                   "WHERE ticker=? ORDER BY ts", (ticker,))]


def _ticks(db, index_id, source, t0, t1):
    return [tuple(r) for r in db.q("SELECT recv_ts, value FROM index_ticks WHERE index_id=? AND source=? "
                                   "AND recv_ts >= ? AND recv_ts <= ? ORDER BY recv_ts", (index_id, source, t0, t1))]


def bad_clock_windows(db) -> list[float]:
    """Minutes where the recorder's clock differed from Kalshi's by more than 2 s (Date headers
    have 1 s resolution, so 1 s of difference is noise)."""
    out = []
    for r in db.q("SELECT ts, data FROM events WHERE kind='recorder_health'"):
        skew = json.loads(r["data"] or "{}").get("clock_skew_s")
        if skew is not None and abs(skew) > 2:
            out.append(r["ts"])
    return sorted(out)


def _clock_ok(m, bad):
    i = bisect.bisect_left(bad, m["open_ts"])
    return not (i < len(bad) and bad[i] <= m["close_ts"])


# --- shared summaries ------------------------------------------------------------------

def summary(rows: list[dict], key: str, level: float = 0.95) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    wins = [r["window"] for r in rows if r.get(key) is not None]
    out = {"n": len(vals), "windows": len(set(wins)), "mean": _mean(vals), "ci": ci(vals, wins, level=level),
           "ci_level": level}
    if vals and len(set(wins)) > 1:
        # Design effect: how much clustering in windows shrinks the effective sample.
        per = defaultdict(list)
        for v, w in zip(vals, wins):
            per[w].append(v)
        mu = sum(vals) / len(vals)
        var = sum((v - mu) ** 2 for v in vals) / len(vals)
        var_w = sum((sum(xs) - len(xs) * mu) ** 2 for xs in per.values()) / len(vals)
        out["design_effect"] = round(var_w / var, 2) if var > 0 else None
    return out


def by(rows: list[dict], label, key: str) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[label(r)].append(r)
    return {str(k): summary(v, key) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def _week(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%G-W%V")


def _us_hours(ts):
    t = datetime.fromtimestamp(ts, ET)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


def _terciles(ms):
    """Cut points of the previous window's move, per series (stratification only, no outcomes)."""
    cuts = {}
    for s in SERIES:
        xs = sorted(m["prev_move"] for m in ms if m["series"] == s and m["prev_move"] is not None)
        cuts[s] = (xs[len(xs) // 3], xs[2 * len(xs) // 3]) if len(xs) >= 3 else None
    return cuts


def _vol_bucket(m, cuts):
    c = cuts.get(m["series"])
    if c is None or m["prev_move"] is None:
        return "unknown"
    return "low" if m["prev_move"] <= c[0] else "mid" if m["prev_move"] <= c[1] else "high"


def _strata(rows, mid_ts, key):
    return {"series": by(rows, lambda r: r["series"], key), "week": by(rows, lambda r: _week(r["window"]), key),
            "half": by(rows, lambda r: "first" if r["window"] < mid_ts else "second", key),
            "prev_window_volatility": by(rows, lambda r: r["vol"], key),
            "us_market_hours": by(rows, lambda r: _us_hours(r["window"]), key),
            "spread": by(rows, lambda r: "1 tick" if r["spread"] <= 0.0101 else "wider", key)}


# --- E2: maker orders --------------------------------------------------------------------

def e2_fills(m: dict, candles: list, trades: list, snaps: list | None = None, *, fill: str = "through",
             latency: float | None = None, spec: dict = SPECS["E2"]) -> list[dict]:
    """E2's rule in one market. At each minute close from 840 s to 60 s left, rest a 1-contract
    bid at the best YES bid and another at the best NO bid, each until the next minute. One fill
    per side per market. Quotes come from the candle close, or from the newest recorded book when
    `snaps` is given (which also gives the queue ahead of us).

    fill: through  only when a taker trades past our price (we must have been hit first)
          queue    trades at our price eat the queue shown when we joined, then fill us
          touch    the first trade at our price fills us (optimistic bound)
    """
    lo, hi = spec["price_band"]
    lat = spec["latency_s"] if latency is None else latency
    times = [t[0] for t in trades]
    snap_ts = [s[0] for s in snaps] if snaps is not None else None
    out, done = [], set()
    for e, yb, ya in candles:
        left = m["close_ts"] - e
        if not spec["quote_until_s_left"] <= left <= spec["quote_from_s_left"]:
            continue
        q_yes = q_no = None
        if snaps is not None:
            i = bisect.bisect_right(snap_ts, e) - 1
            if i < 0 or e - snaps[i][0] > spec["max_book_age_s"]:
                continue
            _, yb, ya, q_yes, q_no = snaps[i]
        if yb is None or ya is None or not 0 < yb < ya < 1:
            continue
        for side, price, queue in (("yes", yb, q_yes), ("no", round(1 - ya, 4), q_no)):
            if side in done or not lo <= price <= hi:
                continue
            ahead = queue or 0.0
            j = bisect.bisect_left(times, e + lat)
            while j < len(trades) and trades[j][0] < e + spec["grid_s"]:
                t, taker, yes_price, count = trades[j]
                j += 1
                if taker == side:          # a taker buying our side never hits our bid
                    continue
                ours = yes_price if side == "yes" else round(1 - yes_price, 4)
                at = abs(ours - price) < 1e-9
                if fill == "queue" and at:
                    ahead -= count
                if ours < price - 1e-9 or (at and (fill == "touch" or (fill == "queue" and ahead < 0))):
                    out.append({"side": side, "price": price, "quote_ts": e, "ts": t, "left": left,
                                "mid": (yb + ya) / 2, "spread": round(ya - yb, 4), "queue": queue})
                    done.add(side)
                    break
    return out


def maker_fee(price: float, mode: str) -> float:
    q = price * (1 - price)
    return {"zero": 0.0, "maker_exact": MAKER_RATE * q, "maker_cent": ceil_cent(MAKER_RATE * q),
            "taker_exact": TAKER_RATE * q}[mode]


def _later_mid(mids, t, y):
    """First mid at or after t; past the close the contract is worth its outcome."""
    i = bisect.bisect_left(mids, (t, -1.0))
    return mids[i][1] if i < len(mids) else float(y)


def _e2_rows(m, fills, mids, cuts):
    rows = []
    for f in fills:
        won = m["y"] if f["side"] == "yes" else 1 - m["y"]
        val = (lambda mid: mid) if f["side"] == "yes" else (lambda mid: 1 - mid)
        r = {**f, "ticker": m["ticker"], "series": m["series"], "window": m["close_ts"], "vol": _vol_bucket(m, cuts),
             "gross": won - f["price"], "spread_earned": val(f["mid"]) - f["price"]}
        r["adverse_selection"] = r["gross"] - r["spread_earned"]
        for h in (10, 60, 300):
            r[f"markout_{h}s"] = val(_later_mid(mids, f["ts"] + h, m["y"])) - f["price"]
        for mode in ("zero", "maker_exact", "maker_cent", "taker_exact"):
            r[f"net_{mode}"] = r["gross"] - maker_fee(f["price"], mode)
        rows.append(r)
    return rows


def e2_report(db: DB, start: float, end: float, level: float, floor: float = PROSPECTIVE_START,
              tickers: set[str] | None = None) -> dict:
    ms = [m for m in markets(db, start, end, floor) if tickers is None or m["ticker"] in tickers]
    cuts = _terciles(ms)
    bad = bad_clock_windows(db)
    variants = defaultdict(list)
    missing = defaultdict(int)
    for m in ms:
        candles, trades = _candles(db, m["ticker"]), _trades(db, m["ticker"])
        if not candles or not trades:
            missing["no candles or trades"] += 1
            continue
        cmids = [(e, (b + a) / 2) for e, b, a in candles if b is not None and a is not None]
        variants["primary"] += _e2_rows(m, e2_fills(m, candles, trades), cmids, cuts)
        variants["latency_5s"] += _e2_rows(m, e2_fills(m, candles, trades, latency=5), cmids, cuts)
        variants["latency_15s"] += _e2_rows(m, e2_fills(m, candles, trades, latency=15), cmids, cuts)
        variants["touch_optimistic"] += _e2_rows(m, e2_fills(m, candles, trades, fill="touch"), cmids, cuts)
        snaps = _snaps(db, m["ticker"])
        if len(snaps) >= 60 and _clock_ok(m, bad):
            smids = [(s[0], (s[1] + s[2]) / 2) for s in snaps if s[1] is not None and s[2] is not None]
            variants["book_through"] += _e2_rows(m, e2_fills(m, candles, trades, snaps), smids, cuts)
            variants["book_queue"] += _e2_rows(m, e2_fills(m, candles, trades, snaps, fill="queue"), smids, cuts)
        else:
            missing["no recorded book (book variants only)"] += 1
    p = variants["primary"]
    mid_ts = start + (end - start) / 2
    rep = {
        "markets": len(ms), "windows": len({m["close_ts"] for m in ms}), "missing": dict(missing),
        "primary": {"net_zero_fee": summary(p, "net_zero", level),
                    **{k: summary(p, k) for k in ("gross", "spread_earned", "adverse_selection", "markout_60s",
                                                  "markout_300s", "net_maker_exact", "net_maker_cent",
                                                  "net_taker_exact")}},
        "variants": {k: {"net_zero_fee": summary(v, "net_zero"), "markout_10s": summary(v, "markout_10s"),
                         "markout_60s": summary(v, "markout_60s")} for k, v in variants.items() if k != "primary"},
        "strata": {**_strata(p, mid_ts, "net_zero"), "side": by(p, lambda r: r["side"], "net_zero"),
                   "time_left": by(p, lambda r: "840-300s" if r["left"] > 300 else "300-120s" if r["left"] > 120
                                   else "120-60s", "net_zero"),
                   "fill_price": by(p, lambda r: "<=0.25" if r["price"] <= 0.25 else ">=0.75" if r["price"] >= 0.75
                                    else "0.25-0.75", "net_zero")},
        "calibration_at_fill": by(p, lambda r: round(math.floor(r["price"] * 10) / 10, 1),
                                  "gross"),
        "bankroll_25_feasible": summary([r for r in p if r["price"] <= 0.25], "net_zero"),
    }
    st = rep["strata"]
    rep["pass"] = _verdict(SPECS["E2"]["pass"], rep["primary"]["net_zero_fee"], [
        summary(p, "net_maker_exact")["mean"], summary(variants["latency_5s"], "net_zero")["mean"]],
        st["half"], st["series"], "min_fills")
    return rep


def _verdict(rule, prim, stress_means, halves, series, n_key) -> dict:
    checks = {
        "ci_lower_above_zero": bool(prim["ci"] and prim["ci"][0] > 0),
        "enough_events": prim["n"] >= rule[n_key] and prim["windows"] >= rule["min_windows"],
        "stress_still_positive": all(x is not None and x > 0 for x in stress_means),
        "both_halves_positive": len(halves) == 2 and all((h["mean"] or 0) > 0 for h in halves.values()),
        "series_positive": sum((s["mean"] or 0) > 0 for s in series.values()) >= rule["min_series_positive"],
    }
    return {"checks": checks, "passed": all(checks.values())}


# --- E1: real index feed -------------------------------------------------------------------

class TickWindow:
    """Ticks for one market window with prefix sums, so volatility and averages are O(1)."""

    def __init__(self, ticks: list[tuple[float, float]]):
        self.recv = [t for t, _ in ticks]
        self.vals = [v for _, v in ticks]
        self.r2, self.dt, self.cum = [0.0], [0.0], [0.0]
        for i, v in enumerate(self.vals):
            self.cum.append(self.cum[-1] + v)
            if i:
                self.r2.append(self.r2[-1] + math.log(v / self.vals[i - 1]) ** 2)
                self.dt.append(self.dt[-1] + self.recv[i] - self.recv[i - 1])

    def forecast(self, t: float, close: float, strike: float, spec: dict) -> float | None:
        """P(YES) from ticks received by t only."""
        j = bisect.bisect_right(self.recv, t)
        if j == 0 or t - self.recv[j - 1] > spec["max_tick_age_s"]:
            return None
        a = bisect.bisect_left(self.recv, t - spec["vol_lookback_s"])
        if j - a < spec["min_ticks"]:
            return None
        dt = self.dt[j - 1] - self.dt[a]
        sigma = math.sqrt((self.r2[j - 1] - self.r2[a]) / dt) if dt > 0 else None
        if not sigma:
            return None
        tau = close - t
        known = None
        if tau <= 60:
            k = bisect.bisect_left(self.recv, close - 60)
            if j > k:
                known = (self.cum[j] - self.cum[k]) / (j - k)
        return prob_settle_yes(self.vals[j - 1], strike, tau, sigma, known)


def e1_points(m: dict, snaps: list, arms: dict[str, TickWindow], spec: dict = SPECS["E1"]) -> list[dict]:
    """One row per checkpoint with a fresh two-sided book, in time order."""
    snap_ts = [s[0] for s in snaps]
    rows = []
    for c in spec["checkpoints_s"]:
        t = m["close_ts"] - c
        i = bisect.bisect_right(snap_ts, t) - 1
        if i < 0 or t - snaps[i][0] > spec["max_book_age_s"]:
            continue
        _, yb, ya = snaps[i][:3]
        if yb is None or ya is None or not 0 < yb < ya < 1:
            continue
        r = {"c": c, "t": t, "bid": yb, "ask": ya, "mid": (yb + ya) / 2, "spread": round(ya - yb, 4)}
        for arm, tw in arms.items():
            r[arm] = tw.forecast(t, m["close_ts"], m["floor_strike"], spec)
        rows.append(r)
    return rows


def blend(coef, mid, q):
    lm = _logit(mid)
    return _sigmoid(coef[0] + coef[1] * lm + coef[2] * (_logit(q) - lm))


def taker_fee(price, mode="exact"):
    x = TAKER_RATE * price * (1 - price)
    return ceil_cent(x) if mode == "cent" else x


def e1_trade(m, rows, snaps, coef, arm, spec=SPECS["E1"], latency=None, slip=0.0, fee_mode="exact"):
    """Rule T1: first checkpoint where blend q beats the ask plus fee by the margin; the order is
    priced at the first book received `latency` s later and skipped if the ask moved past the limit."""
    tr = spec["trade"]
    lat = tr["latency_s"] if latency is None else latency
    snap_ts = [s[0] for s in snaps]
    for r in rows:
        if r.get(arm) is None:
            continue
        qb = blend(coef, r["mid"], r[arm])
        for side, q, ask in (("yes", qb, r["ask"]), ("no", 1 - qb, round(1 - r["bid"], 4))):
            if q - ask - taker_fee(ask) < tr["margin"]:
                continue
            i = bisect.bisect_left(snap_ts, r["t"] + lat)
            if i >= len(snaps) or snaps[i][0] > r["t"] + lat + tr["fill_window_s"]:
                break
            _, yb, ya = snaps[i][:3]
            ex = ya if side == "yes" else (None if yb is None else round(1 - yb, 4))
            if ex is None or not 0 < ex < 1 or q - ex - taker_fee(ex) < tr["margin"]:
                break   # IOC at the limit: no fill here, try the next checkpoint
            px = min(ex + slip, 0.999)
            won = m["y"] if side == "yes" else 1 - m["y"]
            f = taker_fee(px, fee_mode)
            return {"side": side, "c": r["c"], "price": px, "q": q, "fee": f, "gross": won - px,
                    "net": won - px - f, "spread": r["spread"]}
    return None


def e1_mismatch(m, cf_final: list, ev: float | None, arm_ticks: dict) -> dict | None:
    """How far each feed's own final-minute average lands from the official settlement value."""
    if ev is None:
        return None
    step = 10 ** -ROUND_DIGITS[m["series"]]
    out = {}
    for arm, tw in arm_ticks.items():
        k, j = bisect.bisect_left(tw.recv, m["close_ts"] - 60), bisect.bisect_left(tw.recv, m["close_ts"])
        if j > k:
            avg = (tw.cum[j] - tw.cum[k]) / (j - k)
            out[arm] = {"bps": abs(avg / ev - 1) * 1e4, "beyond_step": abs(round(avg, ROUND_DIGITS[m["series"]]) - ev) > step / 2}
    if cf_final:
        out["kalshi_final_avg"] = {"bps": abs(cf_final[-1] / ev - 1) * 1e4,
                                   "beyond_step": abs(cf_final[-1] - ev) > step / 2}
    return out


def e1_periods(db: DB) -> dict | None:
    first = db.one("SELECT MIN(recv_ts) v FROM index_ticks WHERE source='cfbenchmarks' AND recv_ts >= ?",
                   (PROSPECTIVE_START,))["v"]
    if first is None:
        return None
    d0 = math.ceil(first / DAY) * DAY
    sp = SPECS["E1"]
    return {"start": d0, "eval_start": d0 + sp["fit_days"] * DAY,
            "nominal_end": d0 + (sp["fit_days"] + sp["eval_days"]) * DAY}


def _e1_market(db, m, sp, cuts):
    snaps = _snaps(db, m["ticker"])
    arms = {src: TickWindow(_ticks(db, UNDERLYING[m["series"]][pos], src,
                                   m["open_ts"] - sp["vol_lookback_s"] - 60, m["close_ts"]))
            for src, pos in sp["arms"].items()}
    rows = e1_points(m, snaps, arms, sp)
    for r in rows:
        r.update(ticker=m["ticker"], series=m["series"], window=m["close_ts"], y=m["y"], vol=_vol_bucket(m, cuts))
    return snaps, arms, rows


E1_GRID = [(lat, slip, fm) for lat in (1, 3, 5) for slip in (0.0, 0.01, 0.02) for fm in ("exact", "cent")]


def e1_report(db: DB, start: float, eval_start: float, end: float, level: float) -> dict:
    """Pass 1 fits each arm's blend on the fit period. Pass 2 scores the evaluation period with
    those frozen coefficients."""
    sp = SPECS["E1"]
    bad = bad_clock_windows(db)
    fit_ms = [m for m in markets(db, start, eval_start) if _clock_ok(m, bad)]
    eval_ms = [m for m in markets(db, eval_start, end) if _clock_ok(m, bad)]
    cuts = _terciles(eval_ms)
    fit = [r for m in fit_ms for r in _e1_market(db, m, sp, cuts)[2]]
    rep = {"markets": {"fit": len(fit_ms), "evaluation": len(eval_ms)}, "fit_points": len(fit), "arms": {}}
    coefs = {}
    for arm in sp["arms"]:
        f = [r for r in fit if r.get(arm) is not None]
        if len(f) >= 50:
            coefs[arm] = fit_logit([[_logit(r["mid"]), _logit(r[arm]) - _logit(r["mid"])] for r in f],
                                   [r["y"] for r in f])
        else:
            rep["arms"][arm] = {"note": f"not enough fit data: {len(f)} points"}
    ev_rows, mism = [], []
    grid = {arm: defaultdict(list) for arm in coefs}
    for m in eval_ms:
        snaps, arms, rows = _e1_market(db, m, sp, cuts)
        for arm, coef in coefs.items():
            for r in rows:
                if r.get(arm) is not None:
                    r[f"blend_{arm}"] = qb = blend(coef, r["mid"], r[arm])
                    r[f"gain_{arm}"] = _logloss(r["mid"], r["y"]) - _logloss(qb, r["y"])
            for g in E1_GRID:
                t = e1_trade(m, rows, snaps, coef, arm, sp, *g)
                if t:
                    grid[arm][g].append({**t, "window": m["close_ts"], "series": m["series"],
                                         "vol": _vol_bucket(m, cuts)})
        ev_rows += rows
        final = [r["final_avg"] for r in db.q(
            "SELECT final_avg FROM index_ticks WHERE index_id=? AND source='cfbenchmarks' AND recv_ts >= ? "
            "AND recv_ts <= ? AND final_avg IS NOT NULL ORDER BY recv_ts",
            (UNDERLYING[m["series"]][0], m["close_ts"] - 60, m["close_ts"] + 5))]
        ev = json.loads(m["data"] or "{}").get("expiration_value")
        mm = e1_mismatch(m, final, float(str(ev).replace(",", "")) if ev not in (None, "") else None, arms)
        if mm:
            mism.append(mm)
    rep["eval_points"] = len(ev_rows)
    mid_ts = eval_start + (end - eval_start) / 2
    for arm, coef in coefs.items():
        e = [r for r in ev_rows if r.get(f"blend_{arm}") is not None]
        lvl = level if arm == sp["primary_arm"] else 0.95
        fc = (("mid", lambda r: r["mid"]), ("raw", lambda r: r[arm]), ("blend", lambda r: r[f"blend_{arm}"]))
        main = grid[arm][(1, 0.0, "exact")]
        a = {"coef_fit_period": [round(x, 4) for x in coef],
             "log_loss": {k: _mean(_logloss(v(r), r["y"]) for r in e) for k, v in fc},
             "brier": {k: _mean((v(r) - r["y"]) ** 2 for r in e) for k, v in fc},
             "gain_over_mid": summary(e, f"gain_{arm}", lvl),
             "gain_by_checkpoint": by(e, lambda r: r["c"], f"gain_{arm}"),
             "gain_strata": _strata(e, mid_ts, f"gain_{arm}"),
             "calibration": {f"{b / 10:.1f}": {"n": len(g), "blend": _mean(r[f"blend_{arm}"] for r in g),
                                               "mid": _mean(r["mid"] for r in g), "yes_rate": _mean(r["y"] for r in g)}
                             for b in range(10)
                             if (g := [r for r in e if min(int(r[f"blend_{arm}"] * 10), 9) == b])},
             "trades": {"net": summary(main, "net", lvl), "gross": summary(main, "gross"),
                        "fee_per_contract": _mean(t["fee"] for t in main),
                        "by_checkpoint": by(main, lambda t: t["c"], "net"),
                        "strata": _strata(main, mid_ts, "net"),
                        "sensitivity": {f"{lat}s latency, {slip:.2f} slippage, {fm} fee": summary(grid[arm][(lat, slip, fm)], "net")
                                        for lat, slip, fm in E1_GRID}}}
        if arm == sp["primary_arm"]:
            v = _verdict(sp["pass"], a["trades"]["net"],
                         [summary(grid[arm][(3, 0.01, "exact")], "net")["mean"],
                          summary(grid[arm][(1, 0.0, "cent")], "net")["mean"]],
                         a["trades"]["strata"]["half"], a["trades"]["strata"]["series"], "min_trades")
            gain = a["gain_over_mid"]
            v["checks"]["log_loss_gain_ci_above_zero"] = bool(gain["ci"] and gain["ci"][0] > 0)
            v["passed"] = all(v["checks"].values())
            rep["pass"] = v
        rep["arms"][arm] = a
    both = [r for r in ev_rows if r.get("gain_cfbenchmarks") is not None and r.get("gain_coinbase") is not None]
    for r in both:
        r["rti_minus_coinbase"] = r["gain_cfbenchmarks"] - r["gain_coinbase"]
    rep["rti_vs_coinbase_gain"] = summary(both, "rti_minus_coinbase")
    rep["settlement_mismatch"] = {k: {"markets": len(xs), "mean_abs_bps": _mean(x["bps"] for x in xs),
                                      "share_beyond_rounding_step": _mean(float(x["beyond_step"]) for x in xs)}
                                  for k in ("cfbenchmarks", "coinbase", "kalshi_final_avg")
                                  if (xs := [mm[k] for mm in mism if k in mm])}
    cf = rep["settlement_mismatch"].get("cfbenchmarks")
    rep["feature_flagged"] = bool(cf and cf["share_beyond_rounding_step"] > 0.01)
    if "pass" not in rep:
        rep["pass"] = {"passed": False, "checks": {}, "note": "primary arm had too little data"}
    return rep


# --- coverage, gating ----------------------------------------------------------------------

def coverage(db: DB, name: str, start: float, end: float) -> dict:
    """Counts only. No outcome is read here."""
    ms = db.q(f"SELECT ticker, series, open_ts, close_ts FROM markets WHERE result IN ('yes','no') "
              f"AND open_ts >= ? AND open_ts < ? AND series IN ({','.join('?' * len(SERIES))})",
              (max(start, PROSPECTIVE_START), end, *SERIES))
    c, covered = defaultdict(int), []
    for m in ms:
        has_c = db.one("SELECT 1 FROM candles WHERE ticker=? LIMIT 1", (m["ticker"],)) is not None
        has_t = db.one("SELECT 1 FROM trades WHERE ticker=? LIMIT 1", (m["ticker"],)) is not None
        snaps = db.one("SELECT COUNT(*) n FROM snapshots WHERE ticker=?", (m["ticker"],))["n"]
        c["settled_markets"] += 1
        c["with_candles_and_trades"] += has_c and has_t
        c["with_recorded_book"] += snaps >= 60
        if name == "E1":
            rti = db.one("SELECT COUNT(*) n FROM index_ticks WHERE index_id=? AND source='cfbenchmarks' "
                         "AND recv_ts >= ? AND recv_ts < ?", (UNDERLYING[m["series"]][0], m["open_ts"],
                                                              m["close_ts"]))["n"]
            c["with_rti_and_book"] += rti >= 600 and snaps >= 60
        if (rti >= 600 and snaps >= 60) if name == "E1" else (has_c and has_t):
            covered.append(m["open_ts"])
    c["covered"] = len(covered)
    return dict(c), sorted(covered)


def schedule(db: DB, name: str, now: float | None = None) -> dict:
    """Start, end and coverage. The end moves out only when coverage is short, never on results."""
    now = now or time.time()
    sp = SPECS[name]
    if name == "E1":
        p = e1_periods(db)
        if p is None:
            return {"state": "not started", "why": "no CF Benchmarks ticks recorded yet. The recorder needs "
                    "KBOT_PROD_READONLY_KEY_ID and KBOT_PROD_READONLY_PRIVATE_KEY_PATH set on the machine it runs on."}
        span_days = sp["eval_days"]
    else:
        p = {"start": PROSPECTIVE_START, "eval_start": PROSPECTIVE_START,
             "nominal_end": PROSPECTIVE_START + sp["eval_days"] * DAY}
        span_days = sp["eval_days"]
    required = math.ceil(sp["min_coverage"] * span_days * WINDOWS_PER_DAY * len(SERIES))
    cov, covered = coverage(db, name, p["eval_start"], max(now, p["nominal_end"]))
    if sum(t < p["nominal_end"] for t in covered) >= required or now < p["nominal_end"]:
        end = p["nominal_end"]
    elif len(covered) >= required:
        end = covered[required - 1] + 900     # extended by the coverage shortfall
    else:
        end = None                            # still collecting
    return {**p, "end": end, "required_covered_markets": required, "coverage": cov,
            "state": "collecting" if end is None or now < end else "ready for final evaluation"}


def _iso(t):
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def status(db: DB, now: float | None = None) -> dict:
    now = now or time.time()
    out = {"label": LABEL, "now": _iso(now), "prospective_start": _iso(PROSPECTIVE_START)}
    hb = db.get("recorder_heartbeat")
    out["recorder_heartbeat_age_s"] = None if hb is None else round(now - hb)
    fee = db.one("SELECT ts, data FROM events WHERE kind='fee_state' ORDER BY ts DESC LIMIT 1")
    out["latest_fee_state"] = None if fee is None else {"at": _iso(fee["ts"]), **json.loads(fee["data"])}
    for name in SPECS:
        s = schedule(db, name, now)
        out[name] = {k: (_iso(v) if k in ("start", "eval_start", "nominal_end", "end") else v) for k, v in s.items()}
        out[name].update(spec_hash=spec_hash(name), final_openings=db.get(f"experiment_opened:{name}", 0))
    return out


def evaluate(db: DB, name: str, *, final: bool = False, force: bool = False, interim: bool = False,
             now: float | None = None, out_dir: str = "reports") -> dict:
    now = now or time.time()
    sp = SPECS[name]
    s = schedule(db, name, now)
    if s.get("state") == "not started":
        raise SystemExit(f"{name}: evaluation pending. {s['why']}")
    rep = {"label": LABEL, "experiment": name, "spec": sp, "spec_hash": spec_hash(name), "generated_utc": _iso(now)}
    if interim:
        day = PROSPECTIVE_START + sp.get("interim_futility_day", 0) * DAY
        if name != "E2":
            raise SystemExit("only E2 has an interim look, and it can only stop for futility")
        if db.get("experiment_interim:E2"):
            raise SystemExit("E2's interim look was already used")
        if now < day + DAY:
            raise SystemExit(f"E2 interim look opens {_iso(day + DAY)} (day 28 plus one day of backfill)")
        db.set("experiment_interim:E2", now)
        prim = e2_report(db, PROSPECTIVE_START, day, sp["pass"]["ci_level"])["primary"]["net_zero_fee"]
        stop = bool(prim["ci"] and prim["ci"][1] < 0)
        rep["interim"] = {"fills": prim["n"], "windows": prim["windows"],
                          "decision": "STOP for futility" if stop else "continue to the final evaluation"}
        return _write(rep, out_dir, f"experiment-{name}-interim.json")
    ready = s["end"] is not None and now >= s["end"] + 3600   # an hour for the last settlements and backfill
    if not final or (not ready and not force):
        raise SystemExit(f"{name}: evaluation pending. Final evaluation opens "
                         f"{_iso(s['end'] + 3600) if s['end'] else 'once coverage is reached'}; "
                         f"covered {s['coverage']['covered']} of {s['required_covered_markets']} markets so far. "
                         "Run `kbot experiment status` for coverage.")
    opened = db.get(f"experiment_opened:{name}", 0) + 1
    db.set(f"experiment_opened:{name}", opened)
    rep["final_openings"] = opened
    if opened > 1:
        rep["warning"] = f"opened {opened} times; only the first opening is a valid test"
    if not ready:
        rep["warning"] = " ".join(filter(None, ["EARLY LOOK before the end date: not a valid test of the hypothesis.",
                                                rep.get("warning")]))
    end = s["end"] or now
    level = sp["pass"]["ci_level"]
    rep["period"] = {"start": _iso(s["start"]), "eval_start": _iso(s["eval_start"]), "end": _iso(end)}
    rep["results"] = (e1_report(db, s["start"], s["eval_start"], end, level) if name == "E1"
                      else e2_report(db, s["start"], end, level))
    return _write(rep, out_dir, f"experiment-{name}.json")


def _write(rep: dict, out_dir: str, fname: str) -> dict:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(out_dir, fname).write_text(json.dumps(rep, indent=2, default=str))
    return rep

"""Edge audit: why the configured strategy fails to show a positive edge after costs.

Read-only on market data. Every number is HISTORICAL BACKTEST. It answers, in order:
  1. baseline   what the configured strategy earns through the real backtester
  2. checks     whether the backtest leaks future data or mis-prices payoffs, fees, fills
  3. failure    how much of the predicted edge survives: model error vs spread, slippage, fees
  4. baselines  no trade, market-implied, coin flip, and a few rules with an economic reason
  5. calibration  model vs market mid as forecasters, and whether the model adds to the mid
Anything fitted (the market + model blend) is fitted on the train split only.
"""
from __future__ import annotations

import bisect
import json
import math
import random
import time
from collections import defaultdict, namedtuple
from dataclasses import replace
from pathlib import Path

from .backtest import Costs, _book_from_candle, chronological_split, metrics, run
from .collector import load_markets, load_spot
from .config import Settings
from .db import DB
from .feed import UNDERLYING
from .fees import TAKER_RATE, fee
from .model import AVG_WINDOW_S, prob_settle_yes, realized_sigma
from .strategy import Candidate, Snapshot, make

LABEL = "HISTORICAL BACKTEST (simulated fills on Kalshi minute candles)"
SLIP = Costs().slippage
EPS = 1e-4

# One market minute. q = model P(YES); side/qwin/price = the fair_value candidate, if any.
Point = namedtuple("Point", "ticker series window ts tau bid ask y period q sd spot ev side qwin "
                            "next_bid next_ask")


def ci(values, clusters, n_boot: int = 2000, seed: int = 1):
    """95% CI of the mean, resampling whole settlement windows (markets closing together)."""
    s, k = defaultdict(float), defaultdict(int)
    for v, c in zip(values, clusters):
        s[c] += v
        k[c] += 1
    keys = list(s)
    if len(keys) < 5:
        return None
    rng = random.Random(seed)
    stats = sorted(sum(s[c] for c in pick) / sum(k[c] for c in pick)
                   for pick in (rng.choices(keys, k=len(keys)) for _ in range(n_boot)))
    return [round(stats[int(0.025 * n_boot)], 4), round(stats[int(0.975 * n_boot) - 1], 4)]


def _solve(A, b):
    """Gaussian elimination with partial pivoting for the small Newton system."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        for r in range(i + 1, n):
            f = M[r][i] / M[i][i]
            M[r] = [a - f * c for a, c in zip(M[r], M[i])]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


def fit_logit(X, y, iters: int = 8):
    """Logistic regression by Newton steps. Returns [intercept, *coefs]."""
    k = len(X[0]) + 1
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for x, yi in zip(X, y):
            v = (1.0, *x)
            p = _sigmoid(sum(a * b for a, b in zip(w, v)))
            r = p * (1 - p)
            for i in range(k):
                g[i] += (yi - p) * v[i]
                for j in range(k):
                    H[i][j] += r * v[i] * v[j]
        w = [a + b for a, b in zip(w, _solve(H, g))]
    return w


def _sigmoid(z):
    return 1 / (1 + math.exp(-max(-50.0, min(50.0, z))))


def _logit(p):
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def _logloss(p, y):
    p = min(1 - EPS, max(EPS, p))
    return -math.log(p if y else 1 - p)


def _mean(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else None


def decision_points(markets, spot, scfg, period_of, ev) -> list[Point]:
    """Every in-window minute with a two-sided quote, with the Snapshot inputs built the way
    backtest.run builds them (data at or before t only), plus the outcome and the next quote."""
    idx = {k: v[0] for k, v in UNDERLYING.items()}
    spot_ts = {k: [p[0] for p in v] for k, v in spot.items()}
    strat = make("fair_value", replace(scfg, name="fair_value"))
    out = []
    for md in markets:
        m, cs = md.info, md.candles
        sid = idx.get(m.series)
        for i, c in enumerate(cs):
            if not (m.open_ts <= c.end_ts < m.close_ts):
                continue
            book = _book_from_candle(m.ticker, c, 10, c.end_ts)
            if book.mid_yes() is None:
                continue
            t, tau = c.end_ts, m.close_ts - c.end_ts
            snap = Snapshot(now=t, market=m, book=book)
            if sid in spot:
                k = bisect.bisect_right(spot_ts[sid], t)
                pts = spot[sid][max(0, k - 200):k]
                if pts:
                    snap.spot, snap.spot_ts = pts[-1][1], pts[-1][0]
                    snap.sigma = realized_sigma(pts, scfg.vol_lookback_s, now=t, min_points=10)
            q = sd = None
            if snap.spot and snap.sigma and m.floor_strike and tau > AVG_WINDOW_S:
                q = prob_settle_yes(snap.spot, m.floor_strike, tau, snap.sigma)
                sd = snap.sigma * math.sqrt(tau - AVG_WINDOW_S + AVG_WINDOW_S / 3)
            cand = strat.evaluate(snap)
            ok = isinstance(cand, Candidate)
            nxt = cs[i + 1] if i + 1 < len(cs) else None
            out.append(Point(m.ticker, m.series, m.close_ts, t, tau, c.yes_bid, c.yes_ask,
                             1 if md.result == "yes" else 0, period_of[m.ticker], q, sd, snap.spot,
                             ev.get(m.ticker), cand.outcome_side if ok else None,
                             cand.q_win if ok else None, nxt.yes_bid if nxt else None,
                             nxt.yes_ask if nxt else None))
    return out


def _side(p: Point, side: str):
    """(won, ask, market-implied probability) for buying `side` at this point."""
    if side == "yes":
        return p.y, p.ask, (p.bid + p.ask) / 2
    return 1 - p.y, round(1 - p.bid, 4), 1 - (p.bid + p.ask) / 2


def net_per_contract(won, ask, slip=SLIP, exact_fee=False):
    """Buy one contract at ask + slip, hold to settlement."""
    px = ask + slip
    if not 0 < px < 1:
        return None
    f = TAKER_RATE * px * (1 - px) if exact_fee else fee(px, 1)
    return won - px - f


def _rule(points, choose) -> dict:
    """Net per contract for a fixed rule: choose(point) -> side or None. One trade per point given."""
    res = {}
    for per in ("train", "validation", "holdout"):
        rows = []
        for p in points:
            side = p.period == per and choose(p)
            if side:
                won, ask, _ = _side(p, side)
                n = net_per_contract(won, ask)
                if n is not None:
                    rows.append((n, p.window))
        if rows:
            res[per] = {"n": len(rows), "net_per_contract": _mean(r[0] for r in rows),
                        "ci95": ci([r[0] for r in rows], [r[1] for r in rows])}
    return res


def failure_mode(signals) -> dict:
    """Split each signal's predicted edge into what the outcome paid vs the mid, and the costs."""
    out = {}
    for per in ("train", "validation", "holdout"):
        rows = []
        for p in signals:
            if p.period != per:
                continue
            won, ask, mq = _side(p, p.side)
            px = ask + SLIP
            rows.append(dict(won=won, q=p.qwin, mq=mq, ask=ask, fee=fee(px, 1), window=p.window,
                             net=won - px - fee(px, 1), pred=p.qwin - px - fee(px, 1)))
        if not rows:
            continue
        dis = [r["q"] - r["mq"] for r in rows]
        real = [r["won"] - r["mq"] for r in rows]
        mx, my = sum(dis) / len(dis), sum(real) / len(real)
        var = sum((a - mx) ** 2 for a in dis)
        slope = sum((a - mx) * (b - my) for a, b in zip(dis, real)) / var if var else None
        out[per] = {
            "signals": len(rows), "win_rate": _mean(r["won"] for r in rows),
            "model_q": _mean(r["q"] for r in rows), "market_q": _mean(r["mq"] for r in rows),
            "predicted_net": _mean(r["pred"] for r in rows),
            "model_minus_mid": round(mx, 4),
            "realized_minus_mid": round(my, 4),
            "realized_minus_mid_ci95": ci(real, [r["window"] for r in rows]),
            "half_spread": _mean(r["ask"] - r["mq"] for r in rows), "slippage": SLIP,
            "fee": _mean(r["fee"] for r in rows),
            "net": _mean(r["net"] for r in rows), "net_ci95": ci([r["net"] for r in rows], [r["window"] for r in rows]),
            "share_of_disagreement_realized": round(my / mx, 3) if mx else None,
            "slope_realized_on_disagreement": round(slope, 3) if slope is not None else None,
        }
    return out


def cost_sensitivity(signals) -> dict:
    """Same signals under kinder and harsher cost assumptions."""
    cases = {
        "ask+1c, fee rounded up per order (backtest)": lambda w, a, m, n: net_per_contract(w, a),
        "ask, fee rounded up": lambda w, a, m, n: net_per_contract(w, a, 0),
        "ask, exact fee (no rounding)": lambda w, a, m, n: net_per_contract(w, a, 0, True),
        "mid, no fee (zero cost)": lambda w, a, m, n: w - m,
        "next minute's ask+1c (60 s latency)": lambda w, a, m, n: n is not None and net_per_contract(w, n),
    }
    out = {}
    for label, f in cases.items():
        out[label] = {}
        for per in ("train", "validation", "holdout"):
            xs = []
            for p in signals:
                if p.period != per:
                    continue
                won, ask, mq = _side(p, p.side)
                nxt = None
                if p.next_bid and p.next_ask and 0 < p.next_bid < 1 and 0 < p.next_ask < 1:
                    nxt = p.next_ask if p.side == "yes" else round(1 - p.next_bid, 4)
                v = f(won, ask, mq, nxt)
                if v not in (None, False):
                    xs.append(v)
            out[label][per] = _mean(xs)
    return out


def calibration(points, horizons=(600, 300, 120)) -> dict:
    """Brier and log loss of the market mid and the model at fixed minutes left, a train-fitted
    blend of the two, reliability bins, and the model's standardized settlement errors."""
    out = {"by_horizon": {}}
    for tau in horizons:
        h = {}
        for per in ("train", "validation", "holdout"):
            ps = [p for p in points if p.tau == tau and p.period == per and p.q is not None]
            if ps:
                h[per] = {"n": len(ps),
                          "brier_mid": _mean(((p.bid + p.ask) / 2 - p.y) ** 2 for p in ps),
                          "brier_model": _mean((p.q - p.y) ** 2 for p in ps),
                          "logloss_mid": _mean(_logloss((p.bid + p.ask) / 2, p.y) for p in ps),
                          "logloss_model": _mean(_logloss(p.q, p.y) for p in ps)}
        out["by_horizon"][f"{tau}s_left"] = h
    # Blend: y ~ logit(mid) + (logit(model) - logit(mid)). A coefficient of 1 on the second term is
    # what the strategy assumes when it trades on the model; 0 means the model adds nothing.
    use = [p for p in points if p.q is not None and 120 <= p.tau <= 840]
    feats = [(_logit((p.bid + p.ask) / 2), _logit(p.q) - _logit((p.bid + p.ask) / 2)) for p in use]
    tr = [i for i, p in enumerate(use) if p.period == "train"]
    w = fit_logit([feats[i] for i in tr], [use[i].y for i in tr])
    blend = [_sigmoid(w[0] + w[1] * a + w[2] * b) for a, b in feats]
    out["blend"] = {"fitted_on": "train", "coef": {"intercept": round(w[0], 3), "logit_mid": round(w[1], 3),
                                                   "model_minus_mid": round(w[2], 3)}}
    for per in ("train", "validation", "holdout"):
        ii = [i for i, p in enumerate(use) if p.period == per]
        if not ii:
            continue
        gain = [_logloss((use[i].bid + use[i].ask) / 2, use[i].y) - _logloss(blend[i], use[i].y) for i in ii]
        out["blend"][per] = {"points": len(ii),
                             "logloss_mid": _mean(_logloss((use[i].bid + use[i].ask) / 2, use[i].y) for i in ii),
                             "logloss_blend": _mean(_logloss(blend[i], use[i].y) for i in ii),
                             "logloss_model": _mean(_logloss(use[i].q, use[i].y) for i in ii),
                             "gain_per_point": round(sum(gain) / len(gain), 5),
                             "gain_ci95": ci(gain, [use[i].window for i in ii], n_boot=500)}
    # Trading on the blend: first minute per market where it clears ask + 1c + fee.
    first = {}
    for i, p in enumerate(use):
        if p.ticker in first:
            continue
        for side in ("yes", "no"):
            won, ask, _ = _side(p, side)
            qb = blend[i] if side == "yes" else 1 - blend[i]
            n = net_per_contract(qb, ask)
            if n is not None and n > 0:
                first[p.ticker] = (p, side)
                break
    picks = {p.ticker: s for p, s in first.values()}
    out["blend"]["trades"] = _rule([p for p, _ in first.values()], lambda p: picks[p.ticker])
    # Reliability at 5 minutes left, validation: predicted vs realized YES rate per decile.
    rel = {"mid": defaultdict(list), "model": defaultdict(list)}
    for p in points:
        if p.tau == 300 and p.period == "validation" and p.q is not None:
            rel["mid"][min(9, int((p.bid + p.ask) / 2 * 10))].append(((p.bid + p.ask) / 2, p.y))
            rel["model"][min(9, int(p.q * 10))].append((p.q, p.y))
    out["reliability_300s_validation"] = {
        k: [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v), "predicted": _mean(x for x, _ in v),
             "yes_rate": _mean(y for _, y in v)} for b, v in sorted(d.items())] for k, d in rel.items()}
    # Standardized settlement error z = log(settlement / spot) / model sd. N(0, 1) if the model is right.
    zs = defaultdict(list)
    for p in points:
        if p.sd and p.ev and p.spot and p.period == "train":
            zs[p.series].append(math.log(p.ev / p.spot) / p.sd)
    out["settlement_z_train"] = {}
    for ser, z in sorted(zs.items()):
        z.sort()
        med = z[len(z) // 2]
        mad = sorted(abs(x - med) for x in z)[len(z) // 2]
        mu = sum(z) / len(z)
        out["settlement_z_train"][ser] = {
            "n": len(z), "mean": round(mu, 3), "std": round(math.sqrt(sum((x - mu) ** 2 for x in z) / len(z)), 3),
            "robust_std": round(1.4826 * mad, 3), "share_abs_gt_3": round(sum(abs(x) > 3 for x in z) / len(z), 4),
            "normal_share_abs_gt_3": 0.0027}
    return out


def checks(markets, points, base_trades, spot, ev) -> list[dict]:
    """Backtest integrity checks, each with the evidence behind its verdict."""
    out = []
    # Spot timestamps: the stored Coinbase close should match the settlement average best at or
    # just before close time. A best match after close would mean the stored stamp is early (look-ahead).
    idx = {k: v[0] for k, v in UNDERLYING.items()}
    sd = {k: dict(v) for k, v in spot.items()}
    err = {}
    for off in (-120, -60, 0, 60, 120):
        e = [abs(math.log(sd[idx[m.info.series]][m.info.close_ts + off] / ev[m.info.ticker])) * 1e4
             for m in markets if ev.get(m.info.ticker) and m.info.close_ts + off in sd.get(idx[m.info.series], {})]
        err[f"{off:+d}s"] = _mean(e)
    out.append({"check": "look-ahead: spot timestamps", "verdict": "ok",
                "evidence": {"mean_abs_bps_vs_settlement_by_offset": err,
                             "note": "error is lowest at -60 s and 0 s around close and rises after, so stored "
                                     "stamps are not early. Decision code reads candles with end_ts <= t and "
                                     "spot with ts <= t (tests/test_app_backtest.py::test_backtest_no_lookahead)."}})
    seen = defaultdict(int)
    for p in points:
        seen[(p.ticker, p.ts)] += 1
    per_window = defaultdict(set)
    for m in markets:
        per_window[m.info.close_ts].add(m.info.series)
    out.append({"check": "duplicate observations", "verdict": "ok",
                "evidence": {"duplicate_market_minutes": sum(v > 1 for v in seen.values()),
                             "baseline_trades_on_same_market": len(base_trades) - len({t.ticker for t in base_trades}),
                             "markets_per_settlement_window": sorted({len(v) for v in per_window.values()}),
                             "note": "BTC, ETH, SOL and XRP windows close together and their outcomes are "
                                     "correlated, so every CI here resamples whole windows."}})
    mismatch = ties = checked = 0
    for m in markets:
        v, k = ev.get(m.info.ticker), m.info.floor_strike
        if v is None or k is None:
            continue
        checked += 1
        ties += v == k
        mismatch += ("yes" if v >= k else "no") != m.result
    out.append({"check": "settlement labels", "verdict": "ok" if not mismatch else "issue",
                "evidence": {"checked": checked, "mismatches": mismatch, "exact_ties_settled_yes": ties,
                             "note": "result equals expiration_value >= floor_strike for every checkable market."}})
    bad_pnl = max((abs(t.count * (t.side == t.result) - t.count * t.price - t.fees - t.pnl) for t in base_trades),
                  default=0)
    bad_fee = sum(abs(t.fees - fee(t.price, t.count)) > 1e-9 for t in base_trades)
    exact = sum(TAKER_RATE * t.count * t.price * (1 - t.price) for t in base_trades)
    out.append({"check": "YES/NO payoff", "verdict": "ok" if bad_pnl < 1e-9 else "issue",
                "evidence": {"trades_recomputed": len(base_trades), "max_abs_pnl_difference": bad_pnl,
                             "note": "NO is bought at 1 - yes_bid; pays $1 when result is no."}})
    out.append({"check": "fees", "verdict": "ok (pessimistic)",
                "evidence": {"trades_with_fee_not_matching_schedule": bad_fee,
                             "fees_charged": round(sum(t.fees for t in base_trades), 2),
                             "fees_without_cent_rounding": round(exact, 2),
                             "note": "the backtest rounds each order's fee up to the cent. Kalshi rounds per fill "
                                     "to $0.000001 with a rebate accumulator, so real fees are lower for 1 to 3 "
                                     "contract orders. This makes results slightly worse than reality."}})
    moved = [(_side(p, p.side)[1], p.next_ask if p.side == "yes" else 1 - p.next_bid)
             for p in points if p.side and p.next_bid and p.next_ask and 0 < p.next_bid < 1 and 0 < p.next_ask < 1]
    first = {}
    for p in points:
        if p.side and p.ticker not in first:
            first[p.ticker] = p
    sp = [p.ask - p.bid for p in first.values()]
    out.append({"check": "fills and spread", "verdict": "limitation",
                "evidence": {"signal_minutes": len(moved),
                             "next_minute_ask_change_mean": _mean(n - a for a, n in moved),
                             "share_next_ask_up_1c_or_more": _mean(n - a >= 0.01 - 1e-9 for a, n in moved),
                             "mean_spread_at_first_signal": _mean(sp),
                             "note": "fills are at the minute-close ask + 1c with an 80% fill chance drawn "
                                     "independently of the signal. Candles carry no depth, so the 5-contract "
                                     "min_top_size filter never binds. Real IOC orders on a quote that is about "
                                     "to move fail more often than that, so fills are optimistic in that one way."}})
    return out


def audit(db: DB, s: Settings, open_holdout: bool = False, out_dir: str = "reports") -> dict:
    markets = load_markets(db, s.series or None)
    spot = load_spot(db)
    ev = {}
    for r in db.q("SELECT ticker, data FROM markets"):
        v = json.loads(r["data"] or "{}").get("expiration_value")
        if v not in (None, ""):
            ev[r["ticker"]] = float(str(v).replace(",", ""))
    train, valid, hold = chronological_split(markets)
    if not open_holdout:
        markets = train + valid
    period_of = {m.info.ticker: per for per, ms in (("train", train), ("validation", valid), ("holdout", hold))
                 for m in ms}
    idx = {k: v[0] for k, v in UNDERLYING.items()}
    name = s.strategy.name
    rep: dict = {"label": LABEL, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "strategy": name, "bankroll": s.risk.bankroll, "markets": len(markets),
                 "split": {"train": len(train), "validation": len(valid), "holdout": len(hold),
                           "validation_start": time.strftime("%Y-%m-%d %H:%M", time.gmtime(valid[0].info.close_ts)),
                           "holdout_start": time.strftime("%Y-%m-%d %H:%M", time.gmtime(hold[0].info.close_ts))}}
    if open_holdout:
        opened = db.get("holdout_opened_count", 0) + 1
        db.set("holdout_opened_count", opened)
        rep["holdout_opened_count"] = opened
        rep["holdout_warning"] = (f"holdout opened {opened} times. It is not out-of-sample for any rule chosen "
                                  "after the first look. Fresh data starts after the last collected market.")
    # 1. Baseline through the real backtester (risk limits, halts, sizing, fills).
    full = run(markets, name, s.strategy, s.risk, Costs(), spot, idx)
    rep["baseline"] = {"all_periods": metrics(full)}
    for per, ms in (("train", train), ("validation", valid), ("holdout", hold)):
        if per != "holdout" or open_holdout:
            rep["baseline"][per] = metrics(run(ms, name, s.strategy, s.risk, Costs(), spot, idx))
    blocked = {k: v for k, v in full.rejections.items() if k.startswith("risk")}
    rep["baseline"]["decisions_blocked_by_risk"] = {
        "halted": sum(v for k, v in blocked.items() if "halted" in k),
        "budget_too_small": sum(v for k, v in blocked.items() if "too small" in k),
        "other": sum(v for k, v in blocked.items() if "halted" not in k and "too small" not in k)}
    # 2 to 5 on the minute-level table.
    points = decision_points(markets, spot, s.strategy, period_of, ev)
    signals, seen = [], set()
    for p in sorted((p for p in points if p.side), key=lambda p: p.ts):
        if p.ticker not in seen:
            seen.add(p.ticker)
            signals.append(p)
    rep["checks"] = checks(markets, points, full.trades, spot, ev)
    rep["failure_mode"] = {"note": "fair_value's first signal in each market, 1 contract, no risk limits",
                           "by_period": failure_mode(signals), "cost_sensitivity": cost_sensitivity(signals)}
    sig_pick = {p.ticker: p.side for p in signals}
    coin = {}
    for per in ("train", "validation", "holdout"):
        both = [(net_per_contract(*_side(p, "yes")[:2]), net_per_contract(*_side(p, "no")[:2]), p.window)
                for p in signals if p.period == per]
        xs = [(0.5 * (a + b), w) for a, b, w in both if a is not None and b is not None]
        if xs:
            coin[per] = {"n": len(xs), "net_per_contract": _mean(x for x, _ in xs), "ci95": ci(*zip(*xs))}
    bt = {k: metrics(run(markets, k, replace(s.strategy, name=k), s.risk, Costs(), spot, idx))["trades"]
          for k in ("no_trade", "market_implied")}
    rep["baselines"] = {
        "no_trade": {"trades": bt["no_trade"], "net": 0.0},
        "market_implied": {"trades": bt["market_implied"],
                           "note": "trading at the mid's own probability never clears spread and fees"},
        "fair_value_signals": _rule(signals, lambda p: sig_pick[p.ticker]),
        "fair_value_signals_by_series": {ser: _rule([p for p in signals if p.series == ser],
                                                    lambda p: sig_pick[p.ticker])
                                         for ser in sorted({p.series for p in signals})},
        "coin_flip_at_fair_value_times": coin,
        "favorite_mid>=0.80_at_300s": _rule(points, lambda p: p.tau == 300 and (
            "yes" if (p.bid + p.ask) / 2 >= 0.8 else "no" if (p.bid + p.ask) / 2 <= 0.2 else None)),
        "longshot_mid<=0.20_at_300s": _rule(points, lambda p: p.tau == 300 and (
            "no" if (p.bid + p.ask) / 2 >= 0.8 else "yes" if (p.bid + p.ask) / 2 <= 0.2 else None)),
        "xrp_yes_near_0.5_at_600s_(ties_settle_yes)": _rule(points, lambda p: p.tau == 600 and (
            p.series == "KXXRP15M" and 0.4 <= (p.bid + p.ask) / 2 <= 0.6 and "yes")),
    }
    rep["calibration"] = calibration(points)
    _write(rep, out_dir)
    return rep


def _write(rep: dict, out_dir: str) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(out_dir, "edge-audit.json").write_text(json.dumps(rep, indent=2, default=str))

"""Gold token rule, live, for a prospective PAPER test only (research/pyth/spec.json gold_proxy_holdout).

Coinbase PAXG-USD (a gold-backed token) stands in for the Pyth gold index: px = strike * PAXG(now) /
PAXG(open). At 600, 300, 120 and 60 s before close it prices one settlement print, blends that with the
Kalshi mid using the coefficients fit on gold train, and buys 1 contract at the first checkpoint where
the blended chance beats ask + 1c slippage + fee by more than 1c. One signal per market. Every number
below was frozen before gold's held-out periods were read. On them the rule made +2.8c and +5.4c per
contract but missed the pass rule's information clause, so it is not a proven edge and never trades
real money from here.
"""
from __future__ import annotations

import math

from .config import StrategyConfig
from .fees import fee
from .model import prob_settle_yes_print
from .strategy import Candidate, Rejection, Snapshot, build_candidate

TOKEN = {"KXGOLD15M": "PAXG-USD"}        # series -> Coinbase product read as its stand-in
TICK = 0.01                              # gold settles to the cent
CHECKPOINTS = (600, 300, 120, 60)        # seconds left
WINDOW_S = 15                            # take a checkpoint on the first look within 15 s after it
SLIP, MARGIN, MAX_SPREAD = 0.01, 0.01, 0.05
# M1 blend per checkpoint (intercept, logit(mid), logit(q) - logit(mid)), fit on all 2,388 gold train
# markets: reports/pyth-gold-proxy-valid-2026-10-24.json "fits".
FIT = {600: (-0.06344719337027042, 1.167202561374239, 0.37323556804159463),
       300: (0.004728091718586442, 1.2286781751036617, 0.3254032773885892),
       120: (0.03356509380403989, 1.0844676760497265, 0.22264543828764344),
       60: (-0.0014044502634530604, 1.09795049735094, 0.18911517738293002)}


def _lg(p: float) -> float:
    p = min(0.99, max(0.01, p))
    return math.log(p / (1 - p))


def token_features(points: list[tuple[float, float]], now: float, open_ts: float):
    """(price at open, price now, sigma per second) from (ts, price) ticks, or None.

    As in the backtest: minute closes are the last price at or before each whole minute, sigma is the
    root of the summed squared 1-minute log changes over the last hour / 3600, and the rule skips with
    under 30 minutes of data in that hour or a price unchanged for the last 10 minutes.
    shortcut: the backtest's stitched series also jumped at window edges (strike vs token basis); the
    live series does not, so live sigma runs a few % lower and a flat token at 600 s left skips here.
    """
    pts = [(t, v) for t, v in points if t <= now and v > 0]
    if not pts:
        return None
    at_open = [v for t, v in pts if t <= open_ts]
    if not at_open:
        return None
    m0 = int(now // 60) * 60
    closes, seen, i = [], 0, 0
    for b in range(m0 - 3600, m0 + 60, 60):       # 61 minute boundaries, oldest first
        while i < len(pts) and pts[i][0] <= b:
            i += 1
        if i:
            closes.append(pts[i - 1][1])
            seen += any(b - 60 < t <= b for t, _ in pts[max(0, i - 40):i])
    if seen < 30 or len(closes) < 11 or len(set(closes[-11:])) == 1:
        return None
    var = sum(math.log(b / a) ** 2 for a, b in zip(closes, closes[1:]))
    sigma = math.sqrt(var / 3600)
    return (at_open[-1], pts[-1][1], sigma) if sigma > 0 else None


class GoldToken:
    name = "gold_token"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.taken: set[tuple[str, int]] = set()      # (ticker, checkpoint) already looked at
        self.signalled: set[str] = set()

    def evaluate(self, snap: Snapshot) -> Candidate | Rejection:
        m = snap.market
        left = m.seconds_left(snap.now)
        cp = next((c for c in CHECKPOINTS if c - WINDOW_S < left <= c), None)
        if cp is None or m.ticker in self.signalled or (m.ticker, cp) in self.taken:
            return Rejection(m.ticker, self.name, "not at an open checkpoint")
        self.taken.add((m.ticker, cp))
        yb, ya = snap.book.best_bid("yes"), snap.book.best_ask("yes")
        if not yb or not ya or not 0 < yb[0] < ya[0] < 1 or ya[0] - yb[0] > MAX_SPREAD + 1e-9:
            return Rejection(m.ticker, self.name, f"{cp}s: book not two-sided within {MAX_SPREAD}")
        f = token_features(snap.spot_points, snap.now, m.open_ts) if m.floor_strike else None
        if f is None:
            return Rejection(m.ticker, self.name, f"{cp}s: token data missing, short or flat")
        px0, px, sigma = f
        q = prob_settle_yes_print(m.floor_strike * px / px0, m.floor_strike, left, sigma, TICK)
        mid = (yb[0] + ya[0]) / 2
        a, b, c = FIT[cp]
        p = 1 / (1 + math.exp(-(a + b * _lg(mid) + c * (_lg(q) - _lg(mid)))))

        def edge(win, price):
            return win - price - fee(price, 1, fee_type=m.fee_type, multiplier=m.fee_multiplier) if price < 1 else -1

        ey, en = edge(p, ya[0] + SLIP), edge(1 - p, 1 - yb[0] + SLIP)
        why = f"{cp}s: q={q:.3f} mid={mid:.3f} blend={p:.3f} edge yes {ey:+.3f} no {en:+.3f}"
        if max(ey, en) <= MARGIN:
            return Rejection(m.ticker, self.name, why)
        cand = build_candidate(snap, self.cfg, self.name, "yes" if ey >= en else "no", p, SLIP, why, max_count=1)
        if isinstance(cand, Candidate):
            self.signalled.add(m.ticker)
        return cand

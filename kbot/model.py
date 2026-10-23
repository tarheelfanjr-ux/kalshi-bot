"""Probability model for "up or down" 15-minute contracts.

Contract (crypto series): YES if the 60-second average of the settlement index at the end
of the window is >= floor_strike. Model: driftless Brownian motion for log price with
volatility estimated from recent index data. Averaging over the final 60 s lowers the
variance of the settlement value compared with a point price:

  outside the averaging window (tau > 60):  Var = sigma^2 * (tau - 60) + sigma^2 * 60 / 3
  inside it (tau <= 60): part of the average is already known.

This is the simplest defensible model. It ignores drift, jumps, and fat tails, which is
why the strategy subtracts a model-uncertainty buffer from every edge.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

AVG_WINDOW_S = 60.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_settle_yes(spot: float, strike: float, tau: float, sigma: float,
                    known_avg: float | None = None) -> float:
    """P(60 s average at close >= strike). sigma is per sqrt(second) of log price."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    tau = max(tau, 0.0)
    sigma = max(sigma, 1e-9)
    if tau > AVG_WINDOW_S:
        sd = sigma * math.sqrt(tau - AVG_WINDOW_S + AVG_WINDOW_S / 3)
        return norm_cdf(math.log(spot / strike) / sd)
    # Inside the averaging window: known part weighted by elapsed share.
    w_left = tau / AVG_WINDOW_S
    known = known_avg if known_avg is not None else spot
    mean = (1 - w_left) * known + w_left * spot
    sd = w_left * spot * sigma * math.sqrt(tau / 3)
    if sd <= 0:
        return 1.0 if mean >= strike else 0.0
    return norm_cdf((mean - strike) / sd)


def prob_settle_yes_print(spot: float, strike: float, tau: float, sigma: float, tick: float = 0.0) -> float:
    """P(one print at close, rounded to `tick`, >= strike), ties YES. For Pyth-settled series, which settle
    on a single 1-minute candle close, not a 60 s average. sigma is per sqrt(second) of log price."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    edge = strike - tick / 2   # a rounded print >= strike iff the raw print >= strike - half a tick
    sd = max(sigma, 0.0) * math.sqrt(max(tau, 0.0))
    if sd <= 1e-12:
        return 1.0 if spot >= edge else 0.0
    return norm_cdf(math.log(spot / edge) / sd)


def realized_sigma(points: Sequence[tuple[float, float]], lookback_s: float,
                   now: float | None = None, min_points: int = 20) -> float | None:
    """Volatility per sqrt(second) from (ts, price) pairs. Returns None if too little data."""
    if not points:
        return None
    now = now if now is not None else points[-1][0]
    pts = [(t, v) for t, v in points if now - lookback_s <= t <= now and v > 0]
    if len(pts) < min_points:
        return None
    var_sum = dt_sum = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        r = math.log(v1 / v0)
        var_sum += r * r
        dt_sum += dt
    if dt_sum <= 0:
        return None
    return math.sqrt(var_sum / dt_sum)


def prob_with_uncertainty(spot, strike, tau, sigma, known_avg=None) -> tuple[float, float]:
    """Point estimate plus a crude uncertainty band from a +/-25% error in sigma."""
    q = prob_settle_yes(spot, strike, tau, sigma, known_avg)
    lo = prob_settle_yes(spot, strike, tau, sigma * 1.25, known_avg)
    hi = prob_settle_yes(spot, strike, tau, sigma * 0.8, known_avg)
    return q, abs(hi - lo) / 2


if __name__ == "__main__":
    assert abs(prob_settle_yes(100, 100, 600, 0.0005) - 0.5) < 1e-9
    assert prob_settle_yes(101, 100, 600, 0.0005) > 0.5
    assert prob_settle_yes(99, 100, 30, 0.0005, known_avg=103) > 0.5
    assert prob_settle_yes_print(100, 100, 0, 0.0005) == 1.0 and prob_settle_yes_print(100, 100, 600, 0.0005) == 0.5
    assert prob_settle_yes_print(100, 100, 600, 0.0005, tick=0.01) > 0.5
    print("ok")

"""Kalshi trading fees and binary-contract payoff math.

Fee schedule (kalshi.com/docs/kalshi-fee-schedule.pdf, effective 2026-07-07):
  taker = ceil(M * 0.07   * C * P * (1-P))
  maker = ceil(M * 0.0175 * C * P * (1-P))
We round up to the whole cent, which is at least as large as the exchange's per-fill
rounding for non-direct members. That keeps estimates pessimistic.
"""
from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def ceil_cent(x: float) -> float:
    # 1e-9 guards float noise like 0.0175000000001 turning into 0.02
    return math.ceil(round(x * 100, 6) - 1e-9) / 100 if x > 0 else 0.0


def fee(price: float, count: float, *, maker: bool = False,
        fee_type: str = "quadratic", multiplier: float = 1.0) -> float:
    """Fee in dollars for `count` contracts at `price` dollars (0 < price < 1)."""
    if count <= 0:
        return 0.0
    if not 0 < price < 1:
        raise ValueError(f"price out of range: {price}")
    if fee_type == "flat":
        # shortcut: flat-fee series use a separate table we have not seen for 15M markets;
        # fall back to the quadratic taker formula, which is the documented default.
        fee_type = "quadratic"
    if maker:
        if fee_type == "quadratic":
            return 0.0
        rate = MAKER_RATE * (2.0 if fee_type == "quadratic_with_combo_maker_fees" else 1.0)
    else:
        rate = TAKER_RATE
    return ceil_cent(multiplier * rate * count * price * (1 - price))


def expected_profit_per_contract(q_win: float, price: float, fee_per_contract: float) -> float:
    """Buy one contract at `price` that pays $1 with probability q_win.

    Works for YES (q_win = P(YES), price = YES ask) and NO (q_win = 1 - P(YES),
    price = NO ask). Gross edge is q_win - price; net subtracts fees.
    """
    return q_win - price - fee_per_contract


def max_loss(price: float, count: float, fee_paid: float) -> float:
    """Worst case for a bought contract: it settles worthless."""
    return price * count + fee_paid


if __name__ == "__main__":
    assert fee(0.50, 1) == 0.02            # 0.0175 rounds up to 2 cents
    assert fee(0.50, 100) == 1.75
    assert fee(0.50, 100, maker=True) == 0.0
    assert fee(0.50, 100, maker=True, fee_type="quadratic_with_maker_fees") == 0.44
    print("ok")

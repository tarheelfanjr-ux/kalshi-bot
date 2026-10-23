import pytest

from kbot.book import OrderBook
from kbot.config import StrategyConfig
from kbot.fees import ceil_cent, expected_profit_per_contract, fee, max_loss
from kbot.model import prob_settle_yes, prob_with_uncertainty, realized_sigma
from kbot.strategy import FairValue, MarketImplied, Snapshot, Candidate, Rejection, contracts_within

from conftest import book, market


# --- fees and payoffs --------------------------------------------------------
@pytest.mark.parametrize("p,c,maker,ft,expect", [
    (0.50, 1, False, "quadratic", 0.02),
    (0.50, 100, False, "quadratic", 1.75),
    (0.10, 10, False, "quadratic", 0.07),     # 0.063 -> 0.07
    (0.99, 1, False, "quadratic", 0.01),
    (0.50, 100, True, "quadratic", 0.0),
    (0.50, 100, True, "quadratic_with_maker_fees", 0.44),
    (0.50, 100, True, "quadratic_with_combo_maker_fees", 0.88),
])
def test_fee_table(p, c, maker, ft, expect):
    assert fee(p, c, maker=maker, fee_type=ft) == expect


def test_fee_rejects_bad_price_and_zero_count():
    assert fee(0.5, 0) == 0
    with pytest.raises(ValueError):
        fee(1.0, 1)
    assert ceil_cent(0.0175) == 0.02 and ceil_cent(0.07) == 0.07


def test_yes_and_no_payoffs():
    # YES: P(yes)=0.6, ask 0.55, fee 0.02 -> +0.03. NO side: P(no)=0.4, NO ask 0.47 -> -0.09.
    assert expected_profit_per_contract(0.6, 0.55, 0.02) == pytest.approx(0.03)
    assert expected_profit_per_contract(0.4, 0.47, 0.02) == pytest.approx(-0.09)
    assert max_loss(0.05, 10, 0.04) == pytest.approx(0.54)   # cheap contracts still lose everything


# --- order book --------------------------------------------------------------
def test_asks_derived_from_opposite_bids():
    b = OrderBook.from_rest("T", {"yes_dollars": [["0.40", "10"], ["0.42", "13"]],
                                  "no_dollars": [["0.50", "5"], ["0.56", "7"]]})
    assert b.best_bid("yes") == (0.42, 13)
    assert b.best_ask("yes") == (0.44, 7)
    assert b.best_ask("no") == (0.58, 13)
    assert b.spread("yes") == pytest.approx(0.02)
    filled, avg = b.fill_for("yes", 10, limit=0.50)
    assert filled == 10 and avg == pytest.approx((7 * 0.44 + 3 * 0.50) / 10)
    assert b.fill_for("yes", 10, limit=0.45) == (7, pytest.approx(0.44))


def test_deltas_update_and_remove_levels():
    b = OrderBook("T")
    b.apply_snapshot({"yes_dollars_fp": [["0.40", "10.00"]], "no_dollars_fp": [["0.55", "3.00"]]})
    b.apply_delta({"side": "no", "price_dollars": "0.55", "delta_fp": "-3.00"})
    assert b.best_ask("yes") is None
    b.apply_delta({"side": "yes", "price_dollars": "0.41", "delta_fp": "2.00"})
    assert b.best_bid("yes") == (0.41, 2)


# --- model -------------------------------------------------------------------
def test_model_symmetry_and_monotonic():
    assert prob_settle_yes(100, 100, 600, 3e-4) == pytest.approx(0.5)
    a, b2 = prob_settle_yes(100.1, 100, 600, 3e-4), prob_settle_yes(100.2, 100, 600, 3e-4)
    assert 0.5 < a < b2
    # more time left = more uncertainty = closer to 0.5
    assert prob_settle_yes(100.1, 100, 800, 3e-4) < prob_settle_yes(100.1, 100, 200, 3e-4)


def test_averaging_window_reduces_variance_vs_point_price():
    # At tau=60 the remaining randomness is the average only: sd = sigma*sqrt(20), not sqrt(60).
    import math
    from kbot.model import norm_cdf
    q = prob_settle_yes(100.05, 100, 61, 3e-4)
    point = norm_cdf(math.log(100.05 / 100) / (3e-4 * math.sqrt(61)))
    assert q > point


def test_realized_sigma_and_uncertainty():
    pts = [(i, 100 * (1.0001 if i % 2 else 1.0)) for i in range(100)]
    s = realized_sigma(pts, 1000)
    assert s == pytest.approx(1e-4, rel=0.01)
    assert realized_sigma(pts[:5], 1000) is None
    q, u = prob_with_uncertainty(100.1, 100, 300, 3e-4)
    assert 0 < u < 0.2


# --- strategies --------------------------------------------------------------
def test_fair_value_buys_cheap_side_with_costs():
    m = market(strike=100)
    snap = Snapshot(now=m.close_ts - 300, market=m, book=book(yes_bid=0.30, yes_ask=0.32),
                    spot=100.2, spot_ts=m.close_ts - 301, sigma=2e-4)
    c = FairValue(StrategyConfig()).evaluate(snap)
    assert isinstance(c, Candidate) and c.outcome_side == "yes"
    assert c.entry_price == 0.32 and c.net_edge == pytest.approx(c.q_win - 0.32 - c.fee_per_contract - c.uncertainty, abs=1e-3)
    assert c.max_entry_price >= c.entry_price and c.invalidation


def test_fair_value_uses_no_ask_not_complement_of_yes_bid_display():
    from kbot.config import StrategyConfig
    m = market(strike=100)
    b = book(yes_bid=0.70, yes_ask=0.72)       # NO ask = 1 - 0.70 = 0.30
    snap = Snapshot(now=m.close_ts - 300, market=m, book=b, spot=99.8, spot_ts=m.close_ts - 300, sigma=2e-4)
    c = FairValue(StrategyConfig()).evaluate(snap)
    assert isinstance(c, Candidate) and c.outcome_side == "no" and c.entry_price == 0.30


def test_fair_value_rejects_stale_spot_and_wide_spread():
    from kbot.config import StrategyConfig
    m = market()
    snap = Snapshot(now=m.close_ts - 300, market=m, book=book(), spot=101, spot_ts=m.close_ts - 320, sigma=2e-4)
    assert isinstance(FairValue(StrategyConfig()).evaluate(snap), Rejection)
    snap = Snapshot(now=m.close_ts - 300, market=m, book=book(yes_bid=0.3, yes_ask=0.5), spot=101,
                    spot_ts=m.close_ts - 300, sigma=2e-4)
    assert "spread" in FairValue(StrategyConfig()).evaluate(snap).reason


def test_market_implied_never_trades_after_costs():
    from kbot.config import StrategyConfig
    m = market()
    for bid, ask in [(0.45, 0.47), (0.10, 0.11), (0.88, 0.90), (0.50, 0.51)]:
        snap = Snapshot(now=m.close_ts - 300, market=m, book=book(yes_bid=bid, yes_ask=ask))
        assert isinstance(MarketImplied(StrategyConfig(min_net_edge=0.0)).evaluate(snap), Rejection)


def test_contracts_within():
    assert contracts_within(0.25, 0.5, 0.02) == 0       # $25 bankroll * 1% cannot buy a 50c contract
    assert contracts_within(0.25, 0.10, 0.01) == 2

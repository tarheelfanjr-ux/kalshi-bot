"""Gold token paper rule on hand-made ticks and books (no network)."""
import math
import random

import pytest
from conftest import book, market

from kbot.config import Settings, StrategyConfig
from kbot.gold import GoldToken, token_features
from kbot.strategy import Candidate, Snapshot

NOW = 1_800_000_300.0          # a whole minute: 300 s before a 15-minute close


def ticks(start, end, p0=4000.0, sd=2e-5, jump=1.0, jump_at=None, seed=1):
    rng, p, out = random.Random(seed), p0, []
    for t in range(int(start), int(end) + 1, 2):
        p *= math.exp(rng.gauss(0, sd))
        out.append((float(t), p * (jump if jump_at and t >= jump_at else 1.0)))
    return out


def test_token_features_follow_the_backtest_rules():
    pts = ticks(NOW - 4200, NOW)
    px0, px, sigma = token_features(pts, NOW, NOW - 600)
    assert px0 == [v for t, v in pts if t <= NOW - 600][-1] and px == pts[-1][1]
    assert sigma == pytest.approx(2e-5 / math.sqrt(2), rel=0.25)     # 2e-5 per 2 s tick
    assert token_features(pts, NOW, NOW - 5000) is None                # no price at the open yet
    assert token_features(ticks(NOW - 1200, NOW), NOW, NOW - 600) is None   # under 30 minutes of data
    flat = ticks(NOW - 4200, NOW - 900) + [(float(t), 4000.0) for t in range(int(NOW - 898), int(NOW) + 1, 2)]
    assert token_features(flat, NOW, NOW - 600) is None                # unchanged for 10+ minutes


def test_gold_token_signals_once_on_a_token_move_the_book_has_not_priced():
    s = GoldToken(StrategyConfig(name="gold_token", min_net_edge=0.0, max_spread=0.05))
    m = market("KXGOLD15M-T", close_in=300, now=NOW, strike=4000.0, series="KXGOLD15M")
    pts = ticks(NOW - 4200, NOW, jump=1.003, jump_at=NOW - 120)      # token up 0.3% two minutes ago
    snap = Snapshot(now=NOW, market=m, book=book("KXGOLD15M-T", 0.49, 0.50, ts=NOW), spot_points=pts)
    c = s.evaluate(snap)
    assert isinstance(c, Candidate) and c.outcome_side == "yes" and c.count == 1 and c.entry_price == 0.50
    assert "300s:" in c.reason
    assert not isinstance(s.evaluate(snap), Candidate)                    # one signal per market
    later = Snapshot(now=NOW + 180, market=m, book=snap.book, spot_points=pts)
    assert "not at an open checkpoint" in s.evaluate(later).reason


def test_gold_token_skips_wide_books_and_quiet_tokens():
    s = GoldToken(StrategyConfig(name="gold_token", min_net_edge=0.0, max_spread=0.05))
    m = market("KXGOLD15M-W", close_in=300, now=NOW, strike=4000.0, series="KXGOLD15M")
    pts = ticks(NOW - 4200, NOW, jump=1.003, jump_at=NOW - 120)
    wide = s.evaluate(Snapshot(now=NOW, market=m, book=book("KXGOLD15M-W", 0.40, 0.50, ts=NOW), spot_points=pts))
    assert "book" in wide.reason
    m2 = market("KXGOLD15M-Q", close_in=300, now=NOW, strike=4000.0, series="KXGOLD15M")
    wave = [(float(t), 4000 * (1 + 1e-4 * math.sin(2 * math.pi * t / 300))) for t in range(int(NOW - 4200), int(NOW) + 1, 2)]
    calm = s.evaluate(Snapshot(now=NOW, market=m2, book=book("KXGOLD15M-Q", 0.49, 0.50, ts=NOW), spot_points=wave))
    assert not isinstance(calm, Candidate) and "blend=0.4" in calm.reason   # back where it opened: no edge


def test_gold_token_refuses_anything_but_paper(tmp_path):
    from kbot.app import App
    s = Settings(mode="demo", db_path=str(tmp_path / "g.sqlite"))
    s.strategy = StrategyConfig(name="gold_token")
    with pytest.raises(ValueError, match="paper"):
        App(s)

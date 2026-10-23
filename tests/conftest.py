"""Shared fixtures. All market data here is hand-made test input, not real Kalshi data."""
import time

import pytest

from kbot.book import OrderBook
from kbot.config import RiskLimits, Settings, StrategyConfig
from kbot.db import DB
from kbot.discovery import MarketInfo


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def db():
    d = DB(":memory:")
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path):
    s = Settings(mode="paper", db_path=str(tmp_path / "t.sqlite"))
    s.risk = RiskLimits(bankroll=1000.0)
    s.strategy = StrategyConfig()
    return s


def market(ticker="KXBTC15M-T1", close_in=600, now=None, strike=100.0, status="active", series="KXBTC15M"):
    now = now or time.time()
    return MarketInfo(ticker, series, "EV", now + close_in - 900, now + close_in, status, strike,
                      "greater_or_equal", "")


def book(ticker="KXBTC15M-T1", yes_bid=0.45, yes_ask=0.47, size=100, ts=None):
    b = OrderBook(ticker, updated_at=ts or time.time())
    b.yes_bids = {yes_bid: size}
    b.no_bids = {round(1 - yes_ask, 4): size}
    return b


class FakePublic:
    """Stands in for PublicClient. Records every call; never touches the network."""

    def __init__(self, markets=None, books=None):
        self.markets_data = markets or {}
        self.books = books or {}
        self.results = {}
        self.clock_skew_s = 0.0
        self.last_ok = time.time()
        self.calls = []

    async def series_list(self, **_):
        return [{"ticker": "KXBTC15M"}, {"ticker": "KXBTCD"}]

    async def series(self, t):
        return {"ticker": t, "fee_type": "quadratic", "fee_multiplier": 1}

    async def markets(self, **params):
        self.calls.append(("markets", params))
        for m in self.markets_data.values():
            yield m

    async def market(self, t):
        m = dict(self.markets_data[t])
        if t in self.results:
            m.update(status="finalized", result=self.results[t])
        return m

    async def orderbook(self, t, depth=0):
        self.calls.append(("orderbook", t))
        self.last_ok = time.time()
        return self.books[t]

    async def close(self):
        pass


def api_market(ticker, now, close_in=600, strike=100.0):
    return {"ticker": ticker, "event_ticker": "EV", "open_time": iso(now + close_in - 900),
            "close_time": iso(now + close_in), "status": "active", "floor_strike": strike,
            "strike_type": "greater_or_equal", "result": ""}

"""SQLite storage. One writer process, WAL mode, every multi-row change in a transaction.

Every row that holds money carries a `mode` column (paper | demo | production) so
simulated and real results are never mixed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, mode TEXT, kind TEXT NOT NULL, ticker TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, mode TEXT, strategy TEXT, ticker TEXT, outcome_side TEXT,
  entry_price REAL, q_win REAL, market_q REAL, net_edge REAL, count REAL, accepted INTEGER,
  reason TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, mode TEXT NOT NULL, ticker TEXT NOT NULL,
  outcome_side TEXT NOT NULL, limit_price REAL NOT NULL, count REAL NOT NULL, filled REAL DEFAULT 0,
  avg_price REAL, fees REAL DEFAULT 0, state TEXT NOT NULL, purpose TEXT DEFAULT 'entry',
  signal_id INTEGER, created_at REAL, updated_at REAL, last_error TEXT);
CREATE INDEX IF NOT EXISTS orders_state ON orders(mode, state);
CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, client_order_id TEXT, mode TEXT NOT NULL, ticker TEXT, outcome_side TEXT,
  count REAL, price REAL, fee REAL, is_taker INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS positions (
  mode TEXT NOT NULL, ticker TEXT NOT NULL, outcome_side TEXT NOT NULL, count REAL NOT NULL,
  cost REAL NOT NULL, fees REAL NOT NULL, close_ts REAL, status TEXT NOT NULL DEFAULT 'open',
  result TEXT, pnl REAL, opened_at REAL, settled_at REAL,
  PRIMARY KEY (mode, ticker, outcome_side));
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, utc_day TEXT NOT NULL, mode TEXT NOT NULL,
  kind TEXT NOT NULL, ticker TEXT, amount REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_day ON ledger(mode, utc_day);
CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY, series TEXT, event_ticker TEXT, open_ts REAL, close_ts REAL,
  floor_strike REAL, status TEXT, result TEXT, fee_type TEXT, fee_multiplier REAL,
  updated_at REAL, data TEXT);
CREATE INDEX IF NOT EXISTS markets_series_close ON markets(series, close_ts);
CREATE TABLE IF NOT EXISTS candles (
  ticker TEXT NOT NULL, end_ts INTEGER NOT NULL, yes_bid REAL, yes_ask REAL,
  yes_bid_low REAL, yes_ask_high REAL, last REAL, volume REAL, open_interest REAL,
  PRIMARY KEY (ticker, end_ts));
CREATE TABLE IF NOT EXISTS snapshots (
  ts REAL NOT NULL, ticker TEXT NOT NULL, yes_bid REAL, yes_ask REAL, yes_bid_size REAL,
  yes_ask_size REAL, depth TEXT, spot REAL, spot_ts REAL, seconds_left REAL);
CREATE INDEX IF NOT EXISTS snapshots_ticker_ts ON snapshots(ticker, ts);
CREATE TABLE IF NOT EXISTS spot (
  index_id TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL, PRIMARY KEY (index_id, ts));
-- Prospective experiments (kbot/experiments.py). Trades are summed per second, price and taker side.
CREATE TABLE IF NOT EXISTS trades (
  ticker TEXT NOT NULL, ts INTEGER NOT NULL, taker_side TEXT NOT NULL, yes_price REAL NOT NULL,
  count REAL NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (ticker, ts, taker_side, yes_price)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS index_ticks (
  index_id TEXT NOT NULL, source TEXT NOT NULL, recv_ts REAL NOT NULL, src_ts REAL, value REAL NOT NULL,
  avg60 REAL, final_avg REAL, PRIMARY KEY (index_id, source, recv_ts)) WITHOUT ROWID;
-- Maker strategy (kbot/maker.py): every quote the rule posts, with its risk decision and simulated fill.
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY, mode TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL,
  quote_ts REAL NOT NULL, end_ts REAL NOT NULL, yes_bid REAL, yes_ask REAL, queue REAL, seconds_left REAL,
  client_order_id TEXT, risk TEXT, fill_ts REAL, fill_trade_price REAL, status TEXT NOT NULL DEFAULT 'open');
CREATE INDEX IF NOT EXISTS quotes_status ON quotes(mode, status, ticker);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL NOT NULL, mode TEXT NOT NULL, cash REAL, open_value REAL, equity REAL);
"""


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    @contextmanager
    def tx(self):
        """Nested-safe transaction: only the outermost block commits."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT")

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    # --- key/value flags -------------------------------------------------
    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (key, json.dumps(value), time.time()))

    # --- audit log -------------------------------------------------------
    def interruption(self, component: str, heartbeat_key: str, mode: str | None = None,
                     min_gap_s: float = 30) -> float | None:
        """At startup: log how long `component` was down, measured from its last heartbeat."""
        hb = self.get(heartbeat_key)
        if hb is None:
            return None
        gap = time.time() - hb
        if gap >= min_gap_s:
            self.event("interruption", mode, component=component, down_from=hb, down_s=round(gap, 1))
        return gap

    def event(self, kind: str, mode: str | None = None, ticker: str | None = None, **data) -> None:
        self.x("INSERT INTO events(ts,mode,kind,ticker,data) VALUES(?,?,?,?,?)",
               (time.time(), mode, kind, ticker, json.dumps(data, default=str)))

    def ledger(self, mode: str, kind: str, amount: float, ticker: str | None = None,
               ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self.x("INSERT INTO ledger(ts,utc_day,mode,kind,ticker,amount) VALUES(?,?,?,?,?,?)",
               (ts, utc_day(ts), mode, kind, ticker, amount))

    def daily_realized(self, mode: str, day: str | None = None) -> float:
        row = self.one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE mode=? AND utc_day=?",
                       (mode, day or utc_day()))
        return row["s"]

    # --- maintenance -----------------------------------------------------
    def integrity_ok(self) -> bool:
        return self.one("PRAGMA integrity_check")[0] == "ok"

    def backup(self, dest_dir: str = "data/backups") -> str:
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        dest = Path(dest_dir) / f"kbot-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite"
        target = sqlite3.connect(dest)
        with target:
            self.conn.backup(target)
        target.close()
        backups = sorted(Path(dest_dir).glob("kbot-*.sqlite"))
        for old in backups[:-14]:  # keep two weeks of daily backups
            old.unlink()
        return str(dest)

    def close(self):
        self.conn.close()


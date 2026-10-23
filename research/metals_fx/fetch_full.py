"""Re-download every saved market's minute candles with all fields (the collector keeps only bid/ask
closes; this keeps trade price low/high, which the resting-bid fill rule needs). Resumable.

    KBOT_CAND_DB=... KBOT_FULL_DB=... python research/metals_fx/fetch_full.py
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kbot.client import PublicClient  # noqa: E402

from common import CAND_DB, FULL_DB  # noqa: E402

MAX_CANDLES = 10000   # Kalshi counts tickers x minutes per batch call


async def main():
    src, out = sqlite3.connect(CAND_DB), sqlite3.connect(FULL_DB)
    out.execute("CREATE TABLE IF NOT EXISTS raw (ticker TEXT, end_ts INTEGER, j TEXT, PRIMARY KEY(ticker, end_ts))")
    done = {r[0] for r in out.execute("SELECT DISTINCT ticker FROM raw")}
    todo = [r for r in src.execute("SELECT ticker, series, open_ts, close_ts FROM markets ORDER BY series, close_ts")
            if r[0] not in done]
    batches, cur = [], []
    for r in todo:
        trial = cur + [r]
        span = (max(x[3] for x in trial) - min(x[2] for x in trial)) // 60 + 3
        if cur and (r[1] != cur[0][1] or len(trial) * span > MAX_CANDLES):
            batches.append(cur)
            cur = [r]
        else:
            cur = trial
    if cur:
        batches.append(cur)
    print(len(todo), "markets", len(batches), "calls", flush=True)
    client = PublicClient("https://api.elections.kalshi.com/trade-api/v2", read_tokens_per_s=30)  # ~3 req/s
    for i, b in enumerate(batches):
        got = await client.batch_candlesticks([x[0] for x in b], int(min(x[2] for x in b)) - 60,
                                              int(max(x[3] for x in b)) + 60)
        out.executemany("INSERT OR IGNORE INTO raw VALUES(?,?,?)",
                        [(t, int(c["end_period_ts"]), json.dumps(c)) for t, cs in got.items() for c in cs])
        out.commit()
        if i % 50 == 0:
            print(i, len(batches), flush=True)
    print("done", flush=True)


asyncio.run(main())

import json
import math
import random

from kbot.audit import audit, ci, fit_logit, net_per_contract
from kbot.db import DB

from test_app_backtest import synthetic_markets


def test_fit_logit_recovers_coefficients_and_ci_brackets_mean():
    rng = random.Random(3)
    X = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(4000)]
    y = [1 if rng.random() < 1 / (1 + math.exp(-(0.3 + 1.0 * a + 0.0 * b))) else 0 for a, b in X]
    w = fit_logit(X, y)
    assert abs(w[0] - 0.3) < 0.15 and abs(w[1] - 1.0) < 0.15 and abs(w[2]) < 0.15
    vals = [rng.gauss(0.5, 1) for _ in range(400)]
    lo, hi = ci(vals, [i // 4 for i in range(400)])
    assert lo < sum(vals) / len(vals) < hi


def test_net_per_contract_yes_and_no_payoffs():
    # Buy at 0.40 + 1c: fee ceil(0.07 * 0.41 * 0.59) = 2c. Win pays 1.
    assert abs(net_per_contract(1, 0.40) - (1 - 0.41 - 0.02)) < 1e-9
    assert abs(net_per_contract(0, 0.40) - (-0.41 - 0.02)) < 1e-9
    assert net_per_contract(1, 0.995) is None


def test_audit_runs_end_to_end_and_keeps_holdout_closed(settings, tmp_path):
    db = DB(settings.db_path)
    for md in synthetic_markets(80):
        i = md.info
        ev = 100.5 if md.result == "yes" else 99.5
        db.x("INSERT INTO markets(ticker,series,open_ts,close_ts,floor_strike,status,result,data) "
             "VALUES(?,?,?,?,?,?,?,?)", (i.ticker, i.series, i.open_ts, i.close_ts, 100.0, "finalized",
                                         md.result, json.dumps({"expiration_value": f"{ev}"})))
        for c in md.candles:
            db.x("INSERT INTO candles(ticker,end_ts,yes_bid,yes_ask) VALUES(?,?,?,?)",
                 (i.ticker, c.end_ts, c.yes_bid, c.yes_ask))
    for k in range(8000):
        db.x("INSERT INTO spot(index_id,ts,value) VALUES('BRTI',?,?)", (1_700_000_000 + 10 * k, 100.5 + (k % 2) * 0.01))
    rep = audit(db, settings, out_dir=str(tmp_path))
    assert "holdout" not in rep["baseline"] and db.get("holdout_opened_count", 0) == 0
    assert rep["baselines"]["market_implied"]["trades"] == 0
    labels = next(c for c in rep["checks"] if c["check"] == "settlement labels")
    assert labels["evidence"]["mismatches"] == 0 and labels["evidence"]["checked"] == 64
    assert "coef" in rep["calibration"]["blend"]
    assert (tmp_path / "edge-audit.json").exists()

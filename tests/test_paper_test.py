"""The frozen 14-day paper test (P1): restarts and downtime are logged, gaps extend or void the test,
and a change to the risk limits shows."""
import json
import time

from kbot.db import DB
from kbot.maker import PAPER_TEST, paper_test, paper_test_hash

from test_maker import maker_app

DAY = 86400


def test_restart_logs_downtime_unclean_exit_and_the_settings_it_starts_with(tmp_path):
    app, pub, now = maker_app(tmp_path)
    app.db.set("heartbeat", time.time() - 300)
    app.db.set("running", {"pid": 1, "mode": "paper"})        # the last run never shut down cleanly
    app.startup_checks()
    ev = {r["kind"]: json.loads(r["data"]) for r in app.db.q("SELECT kind, data FROM events")}
    assert ev["interruption"]["component"] == "bot" and 299 < ev["interruption"]["down_s"] < 310
    assert "unexpected_termination" in ev
    assert ev["startup"]["limits"] == PAPER_TEST["risk_limits"] and ev["startup"]["paper_test"] == paper_test_hash()


def test_a_second_bot_on_the_same_database_refuses_to_start(tmp_path):
    import os
    import pytest
    app, pub, now = maker_app(tmp_path)
    app.db.set("running", {"pid": os.getppid(), "mode": "paper"})     # a live process
    app.db.set("heartbeat", time.time() - 5)
    with pytest.raises(RuntimeError, match="another bot"):
        app.startup_checks()
    app.db.set("heartbeat", time.time() - 600)                         # stale: that pid is not a bot
    app.startup_checks()


def test_a_short_pause_is_not_an_interruption(tmp_path):
    db = DB(str(tmp_path / "r.sqlite"))
    assert db.interruption("recorder", "recorder_heartbeat") is None      # first start: nothing to compare
    db.set("recorder_heartbeat", time.time() - 5)
    db.interruption("recorder", "recorder_heartbeat")
    db.set("recorder_heartbeat", time.time() - 120)
    db.interruption("recorder", "recorder_heartbeat")
    downs = [json.loads(r["data"]) for r in db.q("SELECT data FROM events WHERE kind='interruption'")]
    assert [d["component"] for d in downs] == ["recorder"] and downs[0]["down_s"] >= 120


def _run(db, t0, minutes, limits=PAPER_TEST["risk_limits"]):
    db.x("INSERT INTO events(ts,mode,kind,ticker,data) VALUES(?,?,?,?,?)",
         (t0, "paper", "startup", None, json.dumps({"limits": limits})))
    for m in minutes:
        db.x("INSERT INTO equity(ts,mode,cash,open_value,equity) VALUES(?,?,?,?,?)", (t0 + 60 * m, "paper", 25, 0, 25))


def test_gaps_extend_the_test_and_a_gap_over_two_hours_voids_it(db):
    t0 = 1_800_000_000
    _run(db, t0, list(range(10)) + list(range(40, 50)))           # down for 30 minutes after minute 9
    pt = paper_test(db, "paper", [], 0.0, now=t0 + 50 * 60)
    assert pt["state"].startswith("collecting") and pt["gaps_over_10_min"] == 1 and pt["longest_gap_min"] == 30
    assert pt["end_utc"] == time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t0 + 14 * DAY + 30 * 60))
    assert pt["risk_limits_unchanged"] and pt["checks"]["no_gap_over_2h"]
    late = paper_test(db, "paper", [], 0.0, now=t0 + 49 * 60 + 3 * 3600)    # nothing for 3 hours since
    assert late["state"].startswith("VOID") and not late["checks"]["no_gap_over_2h"]


def test_stop_loss_ends_early_and_the_end_needs_every_check(db):
    t0 = 1_800_000_000
    _run(db, t0, range(5))
    assert paper_test(db, "paper", [], -5.0, now=t0 + 300)["state"].startswith("FAIL: the account lost $5.00")
    for i in range(60):          # 60 settled 1-contract trades at 0.20 that all won: a clean account result
        db.x("INSERT INTO positions(mode,ticker,outcome_side,count,cost,fees,close_ts,status,result,pnl,opened_at,"
             "settled_at) VALUES('paper',?,'yes',1,0.20,0,?,'settled','yes',0.80,?,?)",
             (f"T{i}", t0 + 900 * (i + 1), t0, t0 + 900 * (i + 1)))
    beats = [t0 + 60 * m for m in range(14 * 24 * 60 + 1)]
    db.x("DELETE FROM equity")
    for b in beats:
        db.x("INSERT INTO equity(ts,mode,cash,open_value,equity) VALUES(?,?,?,?,?)", (b, "paper", 25, 0, 25))
    pt = paper_test(db, "paper", [], 48.0, now=beats[-1] + 30)
    # The account passes, but the research log has no fills: the test fails as a whole.
    assert pt["checks"]["account_ci_above_zero"] and not pt["checks"]["log_300_fills_100_windows"]
    assert pt["state"] == "FAIL"


def test_changed_risk_limits_show_in_the_test(db):
    t0 = 1_800_000_000
    _run(db, t0, range(5))
    _run(db, t0 + 400, range(7, 9), limits={**PAPER_TEST["risk_limits"], "bankroll": 100.0})
    pt = paper_test(db, "paper", [], 0.0, now=t0 + 600)
    assert not pt["risk_limits_unchanged"] and not pt["checks"]["risk_limits_unchanged"]

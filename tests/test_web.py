"""Web control center: shared controls, honest labels, settings, backtest guards and HTTP security.
Everything runs against temp files and a server on an ephemeral local port; nothing touches Kalshi."""
import http.client
import json
import threading
import time

import pytest

from kbot import cli, config as config_mod, control, web
from kbot.config import StrategyConfig
from kbot.db import DB
from kbot.risk import RiskManager
from kbot.strategy import make

CONFIG = """# test config
mode = "paper"   # keep comment
db_path = "{db}"
series = ["KXBTC15M"]

[risk]
bankroll = 100.0   # dollars

[strategy]
name = "fair_value"
"""


@pytest.fixture(autouse=True)
def no_service(tmp_path, monkeypatch):
    """No launchd or systemd service on the test machine, whatever the real home folder holds."""
    monkeypatch.setattr(control, "SERVICE_FILES", (tmp_path / "com.kbot.paper.plist",))
    return tmp_path / "com.kbot.paper.plist"


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG.format(db=tmp_path / "bot.sqlite"))
    return str(p)


@pytest.fixture
def s(cfg):
    return config_mod.load(cfg)


# --- strategies and wording -----------------------------------------------------------------------
def test_strategy_list_is_read_from_the_factory():
    assert {"no_trade", "fair_value", "maker"} <= set(web.STRATEGIES)
    for name in web.STRATEGIES:
        if name != "maker":
            make(name, StrategyConfig())


@pytest.mark.parametrize("n,ci,key", [
    (0, None, "none"), (5, None, "thin"), (300, (-0.05, -0.01), "loses"),
    (300, (0.001, 0.02), "unproven"), (300, (-0.04, 0.04), "no-edge")])
def test_verdict_never_claims_profit(n, ci, key):
    v = web.verdict(n, ci)
    assert v["key"] == key
    assert "profit" not in (v["text"] + v["short"]).lower()


# --- shared controls (the CLI and the web page call the same code) ---------------------------------
def test_pause_resume_and_estop(s):
    db = DB(s.db_path)
    rm = RiskManager(db, s.risk, s.mode)
    control.pause(db, "test")
    assert rm.halted()
    with pytest.raises(control.ControlError):
        control.resume(db, s, "")                      # must say what was checked
    control.resume(db, s, "checked logs")
    assert not rm.halted()
    assert control.estop(db, s, "test") is None        # paper: no exchange call
    with pytest.raises(control.ControlError):
        control.resume(db, s, "checked")               # estop needs an explicit clear
    control.resume(db, s, "checked", clear_estop=True)
    assert db.get("estop") is None and not rm.halted()


def test_cli_pause_writes_the_same_flag(cfg, s):
    cli.main(["--config", cfg, "pause", "--reason", "from cli"])
    assert DB(s.db_path).get("pause")["reason"] == "from cli"
    with pytest.raises(SystemExit):
        cli.main(["--config", cfg, "resume"])


def test_bot_state(s, monkeypatch):
    db = DB(s.db_path)
    now = time.time()
    assert control.bot_state(db, s, now)["state"] == "stopped"
    db.set("running", {"pid": 4242, "mode": "paper", "started": now - 60})
    monkeypatch.setattr(control, "_is_kbot", lambda pid: False)
    st = control.bot_state(db, s, now)
    assert st["state"] == "stopped" and "did not shut down cleanly" in st["detail"]
    monkeypatch.setattr(control, "_is_kbot", lambda pid: True)
    db.set("heartbeat", now - 1)
    db.set("health", {"ok": True, "reason": "ok"})
    assert control.bot_state(db, s, now)["state"] == "running"
    control.pause(db, "test")
    assert control.bot_state(db, s, now)["state"] == "paused"
    db.set("heartbeat", now - 600)
    assert control.bot_state(db, s, now)["state"] == "disconnected"


def test_start_paper_strips_trading_keys(s, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(control.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, env=kw["env"]))
    for k in ("KBOT_DEMO_KEY_ID", "KBOT_PROD_KEY_ID", "KBOT_PROD_PRIVATE_KEY_PATH", "KBOT_PRODUCTION_ARM"):
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("KBOT_PROD_READONLY_KEY_ID", "data-only")
    monkeypatch.setenv("KBOT_MODE", "production")
    control.start_paper("c.toml", s, str(tmp_path / "bot.log"))
    env = seen["env"]
    assert env["KBOT_MODE"] == "paper" and env["KBOT_PROD_READONLY_KEY_ID"] == "data-only"
    assert not [k for k in env if k.startswith(("KBOT_DEMO_KEY", "KBOT_PROD_KEY", "KBOT_PROD_PRIV", "KBOT_PRODUCTION"))]
    assert seen["cmd"][-1] == "run"
    s.mode = "demo"
    with pytest.raises(control.ControlError):
        control.start_paper("c.toml", s)


def test_service_owns_start_and_stop(s, no_service, monkeypatch):
    no_service.write_text("<plist/>")
    db = DB(s.db_path)
    assert control.bot_state(db, s)["service"] == str(no_service)
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **kw: pytest.fail("started a second copy"))
    with pytest.raises(control.ControlError, match="background service"):
        control.start_paper("c.toml", s)
    db.set("running", {"pid": 4242, "mode": "paper"})
    monkeypatch.setattr(control, "_is_kbot", lambda pid: True)
    monkeypatch.setattr(control.os, "kill", lambda *a: pytest.fail("signalled a service-run bot"))
    with pytest.raises(control.ControlError, match="background service"):
        control.stop_process(db)
    control.pause(db, "still works")                     # pause and resume go through the database
    assert db.get("pause")
    assert web.api_settings(db, s, "c.toml")["locked"] == str(no_service)


def test_settings_read_only_under_a_service(server, no_service):
    srv = server()
    no_service.write_text("<plist/>")
    csrf = call(srv, "GET", "/api/session")[1]["csrf"]
    st, body, _ = call(srv, "POST", "/api/settings", {"values": {"risk.bankroll": 50}}, {"X-CSRF-Token": csrf})
    assert st == 409 and "read-only" in body["error"]
    assert config_mod.load(srv.config_path).risk.bankroll == 100.0


# --- settings ---------------------------------------------------------------------------------------
def test_update_toml_keeps_comments_and_adds_keys():
    out = web.update_toml('a = 1  # keep\n\n[risk]\nbankroll = 5.0 # usd\n',
                          {("", "a"): 2, ("risk", "bankroll"): 7.5, ("risk", "max_open_positions"): 3,
                           ("strategy", "name"): "no_trade"})
    assert "a = 2  # keep" in out and "bankroll = 7.5 # usd" in out
    assert "max_open_positions = 3" in out and '[strategy]\nname = "no_trade"' in out


def test_save_settings_validates_and_round_trips(cfg, s):
    with pytest.raises(ValueError, match="not a 15-minute series"):
        web.save_settings(cfg, s, {"series": ["KXBTCD"]})
    with pytest.raises(ValueError, match="cannot be changed"):
        web.save_settings(cfg, s, {"mode": "production"})
    with pytest.raises(ValueError, match="below"):
        web.save_settings(cfg, s, {"strategy.min_seconds_left": 900})
    with pytest.raises(PermissionError):
        web.save_settings(cfg, s, {"risk.bankroll": 1000})
    s2, changed = web.save_settings(cfg, s, {"risk.bankroll": 50, "series": "kxbtc15m, KXETH15M",
                                             "strategy.name": "no_trade"})
    assert changed == {"risk.bankroll": 50.0, "series": ["KXBTC15M", "KXETH15M"], "strategy.name": "no_trade"}
    assert config_mod.load(cfg).risk.bankroll == 50.0 and s2.strategy.name == "no_trade"
    text = open(cfg).read()
    assert "# keep comment" in text and "# dollars" in text
    assert "bankroll = 100.0" in open(cfg + ".bak").read()
    s3, _ = web.save_settings(cfg, s2, {"risk.bankroll": 80}, confirm_raise=True)
    assert s3.risk.bankroll == 80


# --- backtest guards --------------------------------------------------------------------------------
def test_backtest_guards(s, tmp_path):
    b = web.Backtests(str(tmp_path), str(tmp_path))
    b.datasets = lambda: [{"path": "d.sqlite", "series": ["KXBTC15M"]}]
    ok = {"dataset": "d.sqlite", "strategy": "fair_value", "split": "train"}
    assert b.validate(ok, s)["split"] == "train"
    for bad, msg in [({"split": "holdout"}, "holdout"), ({"split": "test"}, "holdout"),
                     ({"strategy": "maker"}, "maker"), ({"dataset": "other.sqlite"}, "dataset"),
                     ({"split": "validation"}, "tune"), ({"slippage": 0.5}, "slippage")]:
        with pytest.raises(ValueError, match=msg):
            b.validate({**ok, **bad}, s)
    assert b.validate({**ok, "split": "validation", "confirm_evaluation": True}, s)["split"] == "validation"


# --- HTTP ---------------------------------------------------------------------------------------------
SECRETS = ("SECRET-KEY-ID", "/secret/key.pem", "SECRET-DATA-ID", "/secret/data.pem", "SECRET-ARM", "hooks.example")


@pytest.fixture
def server(cfg, s, tmp_path):
    started = []

    def make_server(token=""):
        s.api_key_id, s.private_key_path = SECRETS[0], SECRETS[1]
        s.data_key_id, s.data_private_key_path = SECRETS[2], SECRETS[3]
        s.production_arm, s.notify_webhook = SECRETS[4], "https://hooks.example/x"
        srv = web.Server(("127.0.0.1", 0), cfg, s, token, str(tmp_path / "reports"), str(tmp_path / "data"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv
    yield make_server
    for srv in started:
        srv.shutdown()
        srv.server_close()


def call(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        return r.status, json.loads(data), r
    except ValueError:
        return r.status, data, r


def test_local_server_csrf_origin_and_host(server):
    srv = server()
    st, page, r = call(srv, "GET", "/")
    assert st == 200 and b"kbot" in page and "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
    assert call(srv, "GET", "/static/../web.py")[0] == 404
    assert call(srv, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 421
    csrf = call(srv, "GET", "/api/session")[1]["csrf"]
    pause = {"action": "pause"}
    assert call(srv, "POST", "/api/control", pause)[0] == 403
    assert call(srv, "POST", "/api/control", pause, {"X-CSRF-Token": csrf, "Origin": "http://evil.example"})[0] == 403
    assert call(srv, "POST", "/api/control", pause, {"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(srv, "POST", "/api/control", pause, {"X-CSRF-Token": csrf, "Content-Type": "text/plain"})[0] == 415
    ok = {"X-CSRF-Token": csrf}
    assert call(srv, "POST", "/api/control", pause, ok)[0] == 200
    assert srv.db().get("pause")
    assert call(srv, "POST", "/api/control", {"action": "stop"}, ok)[0] == 409       # needs confirm
    assert call(srv, "POST", "/api/control", {"action": "estop"}, ok)[0] == 409      # needs confirm
    assert call(srv, "POST", "/api/control", {"action": "go_live"}, ok)[0] == 409
    st, body, _ = call(srv, "POST", "/api/settings", {"values": {"risk.bankroll": 5000}}, ok)
    assert st == 409 and body["needs_confirm"]
    assert config_mod.load(srv.config_path).risk.bankroll == 100.0


def test_api_never_returns_secrets(server):
    srv = server()
    for path in ("/api/session", "/api/state", "/api/markets", "/api/trades", "/api/strategies",
                 "/api/backtest", "/api/settings"):
        st, body, _ = call(srv, "GET", path)
        assert st == 200, path
        text = json.dumps(body)
        assert not [x for x in SECRETS if x in text], path
    st, body, _ = call(srv, "GET", "/api/state")
    assert body["bot"]["state"] == "stopped" and body["production_trading"] == "disabled"


def test_token_login(server):
    token = "t" * 24
    srv = server(token)
    assert call(srv, "GET", "/")[0] == 200                         # the login page itself
    assert call(srv, "GET", "/api/state")[0] == 401
    assert call(srv, "POST", "/api/login", {"token": "wrong-token-wrong"})[0] == 401
    st, _, r = call(srv, "POST", "/api/login", {"token": token}, {"X-Forwarded-Proto": "https"})
    cookie = r.getheader("Set-Cookie")
    assert st == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
    sid = {"Cookie": cookie.split(";")[0]}
    assert call(srv, "GET", "/api/state", headers=sid)[0] == 200
    csrf = call(srv, "GET", "/api/session", headers=sid)[1]["csrf"]
    assert call(srv, "POST", "/api/control", {"action": "pause"}, sid)[0] == 403
    assert call(srv, "POST", "/api/control", {"action": "pause"}, {**sid, "X-CSRF-Token": csrf})[0] == 200
    srv.failures = [time.time()] * 10
    assert call(srv, "POST", "/api/login", {"token": token})[0] == 429


def test_serve_refuses_unauthenticated_network_bind(cfg, s, monkeypatch):
    monkeypatch.delenv("KBOT_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="non-local"):
        web.serve(cfg, s, host="0.0.0.0", port=0)
    monkeypatch.setenv("KBOT_DASHBOARD_TOKEN", "short")
    with pytest.raises(SystemExit, match="16"):
        web.serve(cfg, s, host="127.0.0.1", port=0)

"""Settings. Loaded from a TOML file plus KBOT_* environment variables (env wins).

Secrets never live in the TOML file. Only the key id and the *path* to the private key
come from the environment.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ENVIRONMENTS = {
    "demo": {
        "rest": "https://external-api.demo.kalshi.co/trade-api/v2",
        "ws": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    },
    "production": {
        "rest": "https://external-api.kalshi.com/trade-api/v2",
        "ws": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    },
}

# The exact phrase an operator must set to arm production. See docs/LIVE_CHECKLIST.md.
PRODUCTION_ARM_PHRASE = "I accept real-money risk"


@dataclass
class RiskLimits:
    bankroll: float = 25.0                  # designated trading bankroll in dollars
    max_position_risk_pct: float = 0.01     # worst-case loss on one position
    max_open_risk_pct: float = 0.05         # worst-case loss across positions + resting orders
    max_daily_loss_pct: float = 0.02        # realized, per UTC day, persisted
    max_consecutive_losses: int = 5
    max_open_positions: int = 3


@dataclass
class StrategyConfig:
    name: str = "fair_value"
    min_net_edge: float = 0.03              # dollars per contract after fees
    min_seconds_left: int = 90              # do not enter inside the final 90 s
    max_seconds_left: int = 840
    max_spread: float = 0.06
    min_top_size: float = 5.0
    vol_lookback_s: int = 900
    model_uncertainty: float = 0.02         # subtracted from edge to cover model error


@dataclass
class Settings:
    # mode: backtest | paper | demo | production
    mode: str = "paper"
    # which Kalshi environment market data comes from (paper uses production data by default)
    data_env: str = "production"
    db_path: str = "data/kbot.sqlite"
    series: list[str] = field(default_factory=list)   # empty = auto-discover *15M series
    max_data_age_s: float = 10.0
    max_clock_skew_s: float = 2.0
    loop_interval_s: float = 2.0
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    notify_webhook: str = ""
    # credentials (from env only)
    api_key_id: str = ""
    private_key_path: str = ""
    data_key_id: str = ""            # optional read-only production key for paper-mode WebSocket data
    data_private_key_path: str = ""
    production_enabled: bool = False
    production_arm: str = ""
    risk: RiskLimits = field(default_factory=RiskLimits)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    @property
    def rest_url(self) -> str:
        return ENVIRONMENTS[self.trade_env]["rest"]

    @property
    def trade_env(self) -> str:
        """The environment orders go to. Paper and backtest never send orders."""
        return "production" if self.mode == "production" else "demo"

    def production_armed(self) -> bool:
        return (
            self.mode == "production"
            and self.production_enabled
            and self.production_arm == PRODUCTION_ARM_PHRASE
        )


def _coerce(value: str, current):
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load(path: str | None = None) -> Settings:
    s = Settings()
    path = path or os.environ.get("KBOT_CONFIG", "config.toml")
    if Path(path).exists():
        raw = tomllib.loads(Path(path).read_text())
        for section, obj in (("risk", s.risk), ("strategy", s.strategy)):
            for k, v in raw.pop(section, {}).items():
                if not hasattr(obj, k):
                    raise ValueError(f"unknown config key {section}.{k}")
                setattr(obj, k, v)
        for k, v in raw.items():
            if k in ("api_key_id", "private_key_path", "production_arm", "data_key_id", "data_private_key_path"):
                raise ValueError(f"{k} must come from the environment, not the config file")
            if not hasattr(s, k):
                raise ValueError(f"unknown config key {k}")
            setattr(s, k, v)
    for f in fields(s):
        env = os.environ.get("KBOT_" + f.name.upper())
        if env is not None and f.name not in ("risk", "strategy", "api_key_id", "private_key_path",
                                                  "data_key_id", "data_private_key_path"):
            setattr(s, f.name, _coerce(env, getattr(s, f.name)))
    # Credentials are per environment so demo keys can never be used for production.
    prefix = "KBOT_PROD_" if s.mode == "production" else "KBOT_DEMO_"
    s.api_key_id = os.environ.get(prefix + "KEY_ID", "")
    s.private_key_path = os.environ.get(prefix + "PRIVATE_KEY_PATH", "")
    s.data_key_id = os.environ.get("KBOT_PROD_READONLY_KEY_ID", "")
    s.data_private_key_path = os.environ.get("KBOT_PROD_READONLY_PRIVATE_KEY_PATH", "")
    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.mode not in ("backtest", "paper", "demo", "production"):
        raise ValueError(f"bad mode {s.mode}")
    if s.data_env not in ENVIRONMENTS:
        raise ValueError(f"bad data_env {s.data_env}")
    if s.mode == "demo" and s.data_env != "demo":
        # Orders in demo must price off demo books, never production books.
        s.data_env = "demo"
    if s.mode == "production" and s.data_env != "production":
        raise ValueError("production mode requires data_env=production")
    r = s.risk
    for name in ("max_position_risk_pct", "max_open_risk_pct", "max_daily_loss_pct"):
        if not 0 < getattr(r, name) <= 0.25:
            raise ValueError(f"risk.{name} must be in (0, 0.25]")
    if r.bankroll <= 0:
        raise ValueError("risk.bankroll must be positive")

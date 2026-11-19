"""Configuration: operating mode, environment settings and owner-only risk limits.

Design rules
- There is no LIVE mode. Any attempt to configure one fails at startup.
- Risk limits are loaded from an owner-controlled TOML file into a frozen model.
  Agents receive the frozen object; there is no API to mutate it at runtime.
- Hard ceilings below are sanity bounds on what the owner may configure. They are
  not recommendations.
"""
from __future__ import annotations

import hashlib
import tomllib
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

OWNER_DEMO_AUTH_PHRASE = "I_AUTHORIZE_DEMO_ORDERS_ONLY"


class Mode(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    BROKER_DEMO = "BROKER_DEMO"
    # LIVE intentionally does not exist.


FORBIDDEN_MODE_NAMES = {"LIVE", "REAL", "PRODUCTION", "PROD"}


class ConfigError(ValueError):
    pass


class RiskLimits(BaseModel):
    """Owner-only limits. Frozen: cannot be mutated after load."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_risk_per_trade_pct: float = Field(gt=0, le=1.0)
    max_total_open_risk_pct: float = Field(gt=0, le=2.0)
    max_daily_drawdown_pct: float = Field(gt=0, le=5.0)
    max_concurrent_positions: int = Field(ge=1, le=3)
    min_reward_risk: float = Field(ge=1.0, le=10.0)
    max_consecutive_losses: int = Field(ge=1, le=5)
    require_stop_loss: bool
    max_spread_points: float = Field(gt=0)
    max_spread_to_stop_ratio: float = Field(gt=0, le=0.5)
    max_margin_usage_pct: float = Field(gt=0, le=30.0)
    min_margin_level_pct: float = Field(ge=300.0)
    max_effective_leverage: float = Field(gt=0, le=20.0)
    max_quote_age_seconds: float = Field(gt=0, le=120)
    max_candle_age_seconds_m5: float = Field(gt=0, le=3600)
    # What to do when macro/news data is UNKNOWN. BROKER_DEMO always denies on UNKNOWN.
    news_unknown_policy: Literal["DENY", "ALLOW_PAPER_ONLY"] = "ALLOW_PAPER_ONLY"
    news_blackout_minutes: int = Field(default=30, ge=0, le=240)
    max_entry_deviation_atr: float = Field(default=0.5, gt=0, le=3.0)

    @field_validator("require_stop_loss")
    @classmethod
    def _sl_mandatory(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("require_stop_loss must be true; trades without stop-loss are not allowed")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "RiskLimits":
        if self.max_total_open_risk_pct < self.max_risk_per_trade_pct:
            raise ValueError("max_total_open_risk_pct must be >= max_risk_per_trade_pct")
        return self


class LoadedRiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    limits: RiskLimits
    source_path: str
    sha256: str


def load_risk_limits(path: str | Path) -> LoadedRiskLimits:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise ConfigError(f"risk limits file not found: {p}")
    raw = p.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    if "limits" not in data:
        raise ConfigError("risk limits file must contain a [limits] table")
    limits = RiskLimits(**data["limits"])
    return LoadedRiskLimits(limits=limits, source_path=str(p), sha256=hashlib.sha256(raw).hexdigest())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tradedesk_mode: str = "PAPER"
    tradedesk_db_path: str = "data/tradedesk.db"
    tradedesk_risk_limits_file: str = "config/risk_limits.toml"
    tradedesk_broker_demo_enabled: bool = False
    tradedesk_owner_demo_authorization: SecretStr = SecretStr("")
    tradedesk_allowed_demo_servers: str = "Bybit-Demo"

    mt5_login: str = ""
    mt5_password: SecretStr = SecretStr("")
    mt5_server: str = ""
    mt5_terminal_path: str = ""

    tradedesk_llm_provider: str = "none"
    tradedesk_llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")

    tradedesk_api_host: str = "127.0.0.1"
    tradedesk_api_port: int = 8000

    @field_validator("tradedesk_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        u = v.strip().upper()
        if u in FORBIDDEN_MODE_NAMES:
            raise ValueError(f"mode {u!r} is forbidden: real-money trading is not implemented and not allowed")
        if u not in Mode.__members__:
            raise ValueError(f"unknown mode {v!r}; allowed: {', '.join(Mode.__members__)}")
        return u

    @property
    def mode(self) -> Mode:
        return Mode(self.tradedesk_mode)

    @property
    def allowed_demo_servers(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.tradedesk_allowed_demo_servers.split(",") if s.strip())

    @property
    def owner_demo_authorized(self) -> bool:
        return self.tradedesk_owner_demo_authorization.get_secret_value() == OWNER_DEMO_AUTH_PHRASE

    def db_path(self) -> Path:
        p = Path(self.tradedesk_db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def public_dict(self) -> dict:
        """Settings safe to show in UI / logs. Secrets are never included."""
        return {
            "mode": self.tradedesk_mode,
            "db_path": str(self.db_path()),
            "risk_limits_file": self.tradedesk_risk_limits_file,
            "broker_demo_enabled": self.tradedesk_broker_demo_enabled,
            "owner_demo_authorized": self.owner_demo_authorized,
            "allowed_demo_servers": list(self.allowed_demo_servers),
            "mt5_login_configured": bool(self.mt5_login),
            "mt5_server": self.mt5_server or None,
            "llm_provider": self.tradedesk_llm_provider,
            "llm_model": self.tradedesk_llm_model or None,
            "llm_key_configured": bool(self.llm_api_key.get_secret_value()),
        }


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)

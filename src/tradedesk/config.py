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

class Mode(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    # LIVE does not exist. BROKER_DEMO was removed by owner directive (2026-11-20):
    # the team trades only inside the internal PaperBroker and never sends orders to any broker.


FORBIDDEN_MODE_NAMES = {"LIVE", "REAL", "PRODUCTION", "PROD", "BROKER_DEMO", "DEMO"}


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
    max_spread_to_atr_ratio: float = Field(default=0.15, gt=0, le=1.0)
    max_weekly_drawdown_pct: float = Field(default=4.0, gt=0, le=10.0)
    max_total_drawdown_pct: float = Field(default=10.0, gt=0, le=30.0)
    max_trades_per_day: int = Field(default=3, ge=1, le=20)
    max_margin_usage_pct: float = Field(gt=0, le=30.0)
    min_margin_level_pct: float = Field(ge=300.0)
    max_effective_leverage: float = Field(gt=0, le=100.0)
    max_quote_age_seconds: float = Field(gt=0, le=120)
    max_candle_age_seconds_m5: float = Field(gt=0, le=3600)
    # What to do when macro/news data is UNKNOWN.
    news_unknown_policy: Literal["DENY", "ALLOW_PAPER_ONLY"] = "ALLOW_PAPER_ONLY"
    news_blackout_minutes: int = Field(default=30, ge=0, le=240)
    max_entry_deviation_atr: float = Field(default=0.5, gt=0, le=3.0)
    # Let strategies without demonstrated OOS edge trade in PAPER, to collect paper evidence.
    # Statistical objections are still recorded (as MEDIUM).
    paper_allow_unproven_strategies: bool = False

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

    # Market data: comma-separated provider chain, tried in order. MOCK is never used unless named explicitly.
    tradedesk_data_source: str = "OANDA,DUKASCOPY"
    tradedesk_symbols: str = "XAUUSD,XAGUSD,EURUSD,GBPUSD,USDJPY,US500"
    tradedesk_quote_poll_seconds: int = Field(default=5, ge=2, le=600)
    oanda_api_token: SecretStr = SecretStr("")
    oanda_account_id: str = ""
    oanda_base_url: str = "https://api-fxpractice.oanda.com"

    # Autonomy: analysis cycles every N minutes while the server runs (0 = only on demand).
    tradedesk_cycle_minutes: int = Field(default=15, ge=0, le=1440)
    tradedesk_autonomous: bool = True
    tradedesk_history_auto: bool = True      # top up Dukascopy history once per UTC day (research only)

    # Internal paper account (house rules of the PaperBroker; not a broker's terms).
    tradedesk_paper_initial_balance: float = Field(default=1000.0, gt=0)
    tradedesk_paper_slippage_points: float = Field(default=2.0, ge=0)
    tradedesk_paper_stop_out_level_pct: float = Field(default=50.0, gt=0, le=1000)

    tradedesk_llm_provider: str = "none"
    tradedesk_llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")

    tradedesk_owner_token: SecretStr = SecretStr("")    # owner actions require X-Owner-Token when set
    tradedesk_access_token: SecretStr = SecretStr("")   # if set, every API/dashboard request needs it

    tradedesk_api_host: str = "127.0.0.1"
    tradedesk_api_port: int = 8000

    @field_validator("tradedesk_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        u = v.strip().upper()
        if u in FORBIDDEN_MODE_NAMES:
            raise ValueError(f"mode {u!r} is forbidden: no real or broker trading; internal PAPER only")
        if u not in Mode.__members__:
            raise ValueError(f"unknown mode {v!r}; allowed: {', '.join(Mode.__members__)}")
        return u

    @model_validator(mode="after")
    def _exposed_needs_token(self) -> "Settings":
        if self.tradedesk_api_host not in ("127.0.0.1", "localhost", "::1") and \
                not self.tradedesk_access_token.get_secret_value():
            raise ValueError("binding to a non-local address requires TRADEDESK_ACCESS_TOKEN")
        return self

    @property
    def mode(self) -> Mode:
        return Mode(self.tradedesk_mode)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip().upper() for s in self.tradedesk_symbols.split(",") if s.strip())

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(s.strip().upper() for s in self.tradedesk_data_source.split(",") if s.strip())

    def db_path(self) -> Path:
        p = Path(self.tradedesk_db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def public_dict(self) -> dict:
        """Settings safe to show in UI / logs. Secrets are never included."""
        return {
            "mode": self.tradedesk_mode,
            "db_path": str(self.db_path()),
            "risk_limits_file": self.tradedesk_risk_limits_file,
            "data_providers": list(self.providers),
            "symbols": list(self.symbols),
            "quote_poll_seconds": self.tradedesk_quote_poll_seconds,
            "oanda_token_configured": bool(self.oanda_api_token.get_secret_value()),
            "cycle_minutes": self.tradedesk_cycle_minutes,
            "autonomous": self.tradedesk_autonomous,
            "paper_initial_balance": self.tradedesk_paper_initial_balance,
            "paper_slippage_points": self.tradedesk_paper_slippage_points,
            "paper_stop_out_level_pct": self.tradedesk_paper_stop_out_level_pct,
            "llm_provider": self.tradedesk_llm_provider,
            "llm_model": self.tradedesk_llm_model or None,
            "llm_key_configured": bool(self.llm_api_key.get_secret_value()),
            "owner_token_required": bool(self.tradedesk_owner_token.get_secret_value()),
            "access_token_required": bool(self.tradedesk_access_token.get_secret_value()),
        }


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)

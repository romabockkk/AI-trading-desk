"""Desk configuration.

Risk limits mirror RISK_MANAGEMENT.md and may only be changed by the user.
Secrets are read from environment variables (or a local, git-ignored .env file)
and are never written to logs, reports or the journal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 1.0        # % of demo equity per trade
    min_rr: float = 2.0                    # minimum planned reward/risk
    max_open_positions: int = 1
    max_consecutive_losses: int = 2        # StoplossGuard: stop new trades for the day
    max_daily_drawdown_pct: float = 2.0    # stop trading for the day
    cooldown_minutes_after_close: int = 60 # CooldownPeriod after any closed trade
    max_leverage: int = 5                  # leverage is NOT risk; it only caps margin use
    max_data_age_minutes: int = 20         # stale data => NOT ENOUGH DATA


@dataclass(frozen=True)
class StrategyParams:
    version: str = "1.0"
    swing_lookback: int = 3                # fractal pivot half-width (bars)
    sweep_window_h1: int = 12              # H1 bars in which a liquidity sweep must have happened
    confirm_window_m15: int = 24           # M15 bars after the sweep to find CHoCH
    min_rr: float = 2.0                    # same as RiskLimits.min_rr (targets are built for it)
    sl_atr_buffer: float = 0.15            # SL buffer beyond the sweep extreme, in M15 ATR
    max_rr_target: float = 4.0             # cap TP at this R if liquidity is farther
    entry_valid_bars_m15: int = 8          # limit entry expires after N M15 bars (2h)


@dataclass(frozen=True)
class CostModel:
    maker_fee: float = 0.0002              # OKX swap regular tier, verify for your account
    taker_fee: float = 0.0005
    slippage: float = 0.0002               # applied to stop / market fills
    funding_per_8h: float = 0.0001         # used only in backtests


@dataclass
class Settings:
    inst_ids: list[str] = field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    risk: RiskLimits = field(default_factory=RiskLimits)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    costs: CostModel = field(default_factory=CostModel)
    leverage: int = 3
    margin_mode: str = "isolated"
    okx_base_url: str = "https://www.okx.com"
    state_dir: Path = Path("state")
    workspace_dir: Path = Path("workspace")
    require_news: bool = False             # True => no connected news source = NOT ENOUGH DATA
    llm_enabled: bool = False              # optional Claude review layer
    llm_model: str = "claude-opus-5-5"

    # --- credentials (demo only) ---
    @property
    def okx_api_key(self) -> str | None:
        return os.environ.get("OKX_DEMO_API_KEY") or None

    @property
    def okx_secret(self) -> str | None:
        return os.environ.get("OKX_DEMO_SECRET_KEY") or None

    @property
    def okx_passphrase(self) -> str | None:
        return os.environ.get("OKX_DEMO_PASSPHRASE") or None

    @property
    def has_demo_credentials(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret and self.okx_passphrase)

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or Path.cwd()
        _load_dotenv(root / ".env")
        s = cls()
        pairs = os.environ.get("DESK_INST_IDS")
        if pairs:
            s.inst_ids = [p.strip() for p in pairs.split(",") if p.strip()]
        if os.environ.get("DESK_LEVERAGE"):
            s.leverage = int(os.environ["DESK_LEVERAGE"])
        s.leverage = max(1, min(s.leverage, s.risk.max_leverage))
        s.require_news = os.environ.get("DESK_REQUIRE_NEWS", "0") == "1"
        s.llm_enabled = os.environ.get("DESK_LLM", "0") == "1"
        s.state_dir = root / "state"
        s.workspace_dir = root / "workspace"
        return s

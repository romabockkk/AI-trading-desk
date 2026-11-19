"""Configuration loading (TOML, stdlib only)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(os.environ.get("GROK_BUDGET_CONFIG", "grok_config.toml"))
DEFAULT_STATE_DIR = Path(os.environ.get("GROK_BUDGET_STATE", "grok_state"))


@dataclass
class Bot:
    name: str
    label: str
    team: str
    priority: int = 3
    min_share: float = 0.0
    max_share: float = 100.0
    protected: bool = False
    max_output_tokens: int | None = None
    model: str | None = None


@dataclass
class Config:
    weekly_budget_usd: float
    reserve_percent: float
    plan_days: float
    enforcement: str
    dedupe_window_minutes: float
    retention_days: int
    base_url: str
    api_key_env: str
    default_model: str
    timeout_seconds: float
    pricing: dict[str, dict[str, float]]
    analysis: dict[str, float]
    teams: dict[str, str]
    bots: dict[str, Bot]
    state_dir: Path = field(default=DEFAULT_STATE_DIR)

    @property
    def distributable_usd(self) -> float:
        """Budget that is actually split between bots (weekly minus reserve)."""
        return self.weekly_budget_usd * (1 - self.reserve_percent / 100)

    @property
    def reserve_usd(self) -> float:
        return self.weekly_budget_usd - self.distributable_usd

    def price(self, model: str) -> dict[str, float]:
        if model in self.pricing:
            return self.pricing[model]
        # unknown model: fall back to the most expensive known price (safe side)
        if self.pricing:
            return max(self.pricing.values(), key=lambda p: p.get("output", 0))
        return {"input": 0.0, "output": 0.0}

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = self.price(model)
        return (prompt_tokens * p.get("input", 0) + completion_tokens * p.get("output", 0)) / 1_000_000

    def model_for(self, bot: str) -> str:
        b = self.bots.get(bot)
        return (b.model if b and b.model else None) or self.default_model


_ANALYSIS_DEFAULTS = {
    "min_calls_for_verdict": 10,
    "waste_reduce_percent": 30,
    "waste_disable_percent": 80,
    "duplicate_cache_percent": 20,
    "long_prompt_tokens": 8000,
}


def load_config(path: str | Path = DEFAULT_CONFIG, state_dir: str | Path | None = None) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    budget = raw.get("budget", {})
    api = raw.get("api", {})
    teams = {k: v.get("label", k) for k, v in raw.get("teams", {}).items()}
    bots: dict[str, Bot] = {}
    for name, b in raw.get("bots", {}).items():
        team = b.get("team", "default")
        teams.setdefault(team, team)
        bots[name] = Bot(
            name=name,
            label=b.get("label", name),
            team=team,
            priority=int(b.get("priority", 3)),
            min_share=float(b.get("min_share", 0)),
            max_share=float(b.get("max_share", 100)),
            protected=bool(b.get("protected", False)),
            max_output_tokens=b.get("max_output_tokens"),
            model=b.get("model"),
        )

    cfg = Config(
        weekly_budget_usd=float(budget.get("weekly_budget_usd", 10)),
        reserve_percent=float(budget.get("reserve_percent", 15)),
        plan_days=float(budget.get("plan_days", 8)),
        enforcement=str(budget.get("enforcement", "hard")),
        dedupe_window_minutes=float(budget.get("dedupe_window_minutes", 30)),
        retention_days=int(budget.get("retention_days", 28)),
        base_url=str(api.get("base_url", "https://api.x.ai/v1")).rstrip("/"),
        api_key_env=str(api.get("api_key_env", "XAI_API_KEY")),
        default_model=str(api.get("default_model", "grok-4")),
        timeout_seconds=float(api.get("timeout_seconds", 120)),
        pricing={k: {kk: float(vv) for kk, vv in v.items()} for k, v in raw.get("pricing", {}).items()},
        analysis={**_ANALYSIS_DEFAULTS, **raw.get("analysis", {})},
        teams=teams,
        bots=bots,
        state_dir=Path(state_dir) if state_dir else DEFAULT_STATE_DIR,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not 0 <= cfg.reserve_percent < 100:
        raise ValueError("reserve_percent must be in [0, 100)")
    if cfg.plan_days < 1:
        raise ValueError("plan_days must be >= 1")
    if cfg.enforcement not in ("hard", "soft"):
        raise ValueError("enforcement must be 'hard' or 'soft'")
    total_min = sum(b.min_share for b in cfg.bots.values())
    if total_min > 100:
        raise ValueError(f"sum of min_share is {total_min}% (> 100%)")
    for b in cfg.bots.values():
        if b.min_share > b.max_share:
            raise ValueError(f"bot {b.name}: min_share > max_share")

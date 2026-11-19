from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import UtcDatetime


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class GateResult(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class TradeProposal(BaseModel):
    """What the Chief hands to Risk Manager / Risk Gate. Prices are proposals, not orders."""
    model_config = ConfigDict(frozen=True)
    decision_id: str
    cycle_id: str
    symbol: str
    action: Action
    entry: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_ids: tuple[str, ...] = ()
    created_utc: UtcDatetime

    @model_validator(mode="after")
    def _geometry(self) -> "TradeProposal":
        if self.action is Action.NO_TRADE:
            return self
        if self.entry is None or self.stop_loss is None:
            raise ValueError("LONG/SHORT requires entry and stop_loss")
        if self.action is Action.LONG:
            if not self.stop_loss < self.entry:
                raise ValueError("LONG stop_loss must be below entry")
            if self.take_profit is not None and not self.take_profit > self.entry:
                raise ValueError("LONG take_profit must be above entry")
        else:
            if not self.stop_loss > self.entry:
                raise ValueError("SHORT stop_loss must be above entry")
            if self.take_profit is not None and not self.take_profit < self.entry:
                raise ValueError("SHORT take_profit must be below entry")
        return self

    @property
    def reward_risk(self) -> float | None:
        if self.action is Action.NO_TRADE or self.take_profit is None:
            return None
        return abs(self.take_profit - self.entry) / abs(self.entry - self.stop_loss)


class GateCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    passed: bool
    detail: str


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    result: GateResult
    checks: tuple[GateCheck, ...]
    volume_lots: float | None = None
    risk_money: float | None = None
    risk_limits_sha256: str

    @property
    def failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

"""Strict JSON report schemas for Chief and the seven agents.

Every agent output must validate against its payload model; anything else is
recorded as INVALID_OUTPUT. A failed critical agent forces NO_TRADE.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import UtcDatetime
from .trading import Action


class AgentName(str, Enum):
    CHIEF = "CHIEF"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    QUANT = "QUANT"
    MACRO = "MACRO"
    RISK_MANAGER = "RISK_MANAGER"
    SKEPTIC = "SKEPTIC"
    EXECUTION = "EXECUTION"
    PERFORMANCE = "PERFORMANCE"


CRITICAL_AGENTS = frozenset({
    AgentName.CHIEF, AgentName.MARKET_STRUCTURE, AgentName.MACRO,
    AgentName.RISK_MANAGER, AgentName.SKEPTIC,
})


class ReportStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    SKIPPED = "SKIPPED"


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(_Strict):
    name: str
    url: str | None = None
    published_utc: UtcDatetime | None = None   # None => publication time unknown
    retrieved_utc: UtcDatetime


class StructureEvent(_Strict):
    timeframe: str
    kind: Literal["SWING_HIGH", "SWING_LOW", "HH", "HL", "LH", "LL", "BOS", "CHOCH",
                  "ORDER_BLOCK", "FVG", "LIQUIDITY_SWEEP"]
    direction: Literal["BULLISH", "BEARISH", "NONE"]
    time_utc: UtcDatetime
    price_low: float
    price_high: float
    rule_id: str                    # id of the formal definition that produced it


class MarketStructurePayload(_Strict):
    bias_by_timeframe: dict[str, Bias]
    events: list[StructureEvent]
    summary: str


class QuantPayload(_Strict):
    regime: Literal["TRENDING", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY", "UNKNOWN"]
    signals: list[dict]             # {strategy_id, version, action, entry, sl, tp, oos_metrics_ref}
    evidence_experiment_ids: list[str]
    summary: str


class MacroEvent(_Strict):
    name: str                       # CPI, NFP, FOMC ...
    scheduled_utc: UtcDatetime | None
    impact: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    source: SourceRef


class MacroPayload(_Strict):
    usd_bias: Bias
    gold_bias: Bias
    upcoming_events: list[MacroEvent]
    blackout_active: bool | None    # None => UNKNOWN
    data_status: Literal["OK", "PARTIAL", "UNKNOWN"]
    summary: str


class RiskManagerPayload(_Strict):
    verdict: Literal["APPROVE", "VETO"]
    volume_lots: float | None
    risk_money: float | None
    risk_pct: float | None
    reasons: list[str]


class Objection(_Strict):
    id: str
    category: Literal["DATA", "STATISTICS", "OVERFITTING", "CORRELATION", "NEWS", "CONTRADICTION",
                      "EXECUTION", "STRUCTURE", "OTHER"]
    text: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class SkepticPayload(_Strict):
    objections: list[Objection]
    demand_no_trade: bool
    summary: str


class ChiefPayload(_Strict):
    hypothesis: str
    alternative: str
    proposed_action: Action
    final_action: Action | None = None
    reasons: list[str]
    unresolved_objection_ids: list[str] = Field(default_factory=list)


class ExecutionPayload(_Strict):
    venue: Literal["PAPER", "BROKER_DEMO", "NONE"]
    client_order_id: str | None
    status: str
    broker_confirmation: dict | None
    reconciliation_ok: bool | None


class PerformancePayload(_Strict):
    trade_id: str | None
    expected_r: float | None
    actual_r: float | None
    error_categories: list[str]
    lessons: list[str]


PAYLOAD_MODELS: dict[AgentName, type[BaseModel]] = {
    AgentName.CHIEF: ChiefPayload,
    AgentName.MARKET_STRUCTURE: MarketStructurePayload,
    AgentName.QUANT: QuantPayload,
    AgentName.MACRO: MacroPayload,
    AgentName.RISK_MANAGER: RiskManagerPayload,
    AgentName.SKEPTIC: SkepticPayload,
    AgentName.EXECUTION: ExecutionPayload,
    AgentName.PERFORMANCE: PerformancePayload,
}


class AgentReport(_Strict):
    report_id: str
    cycle_id: str
    agent: AgentName
    created_utc: UtcDatetime
    status: ReportStatus
    confidence: float | None = Field(default=None, ge=0, le=1)
    snapshot_ids: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    payload: dict | None = None
    error: str | None = None
    llm_review: dict | None = None
    attempts: int = 1
    latency_ms: int | None = None

    @property
    def is_critical(self) -> bool:
        return self.agent in CRITICAL_AGENTS

    def parsed_payload(self) -> BaseModel:
        if self.status is not ReportStatus.OK or self.payload is None:
            raise ValueError(f"report {self.report_id} has no valid payload (status={self.status})")
        return PAYLOAD_MODELS[self.agent].model_validate(self.payload)


def critical_failure(reports: list[AgentReport]) -> list[AgentName]:
    """Critical agents that are missing or not OK. Non-empty => NO_TRADE."""
    ok = {r.agent for r in reports if r.status is ReportStatus.OK}
    return sorted((a for a in CRITICAL_AGENTS if a not in ok), key=lambda a: a.value)

"""Structured messages exchanged between agents (TradingAgents-style structured outputs)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


class Decision(str, Enum):
    APPROVED_FOR_DEMO = "APPROVED FOR DEMO"
    REJECTED = "REJECTED"
    WAIT_FOR_CONFIRMATION = "WAIT FOR CONFIRMATION"
    NOT_ENOUGH_DATA = "NOT ENOUGH DATA"


class Stance(str, Enum):
    SUPPORT = "SUPPORT"
    NEUTRAL = "NEUTRAL"
    OPPOSE = "OPPOSE"
    VETO = "VETO"
    UNAVAILABLE = "UNAVAILABLE"


class Severity(str, Enum):
    INFO = "INFO"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


@dataclass
class Objection:
    text: str
    severity: Severity
    evidence: str = ""


@dataclass
class TradeProposal:
    inst_id: str
    direction: str                 # LONG | SHORT
    entry: float
    stop_loss: float
    take_profit: float
    setup: dict[str, Any]          # H4 bias, sweep, CHoCH, zone ... (the evidence)
    reasoning: list[str]
    created_at: str = field(default_factory=lambda: iso(utcnow()))
    position_size_contracts: float | None = None
    risk_usd: float | None = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def rr(self) -> float:
        r = self.risk_per_unit
        return abs(self.take_profit - self.entry) / r if r > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rr"] = round(self.rr, 2)
        return d


@dataclass
class AgentReport:
    """One entry in the shared workspace. Fields follow AGENT_WORKFLOW.md §1."""
    agent: str
    inst_id: str
    stance: Stance
    confidence: float                       # 0..1
    findings: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    data_timestamp: str | None = None
    objections: list[Objection] = field(default_factory=list)
    recommended_action: str = ""
    proposal: TradeProposal | None = None
    timestamp: str = field(default_factory=lambda: iso(utcnow()))
    simulated_role: bool = False            # True only for LLM role-play output

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stance"] = self.stance.value
        d["objections"] = [{"text": o.text, "severity": o.severity.value, "evidence": o.evidence}
                           for o in self.objections]
        d["proposal"] = self.proposal.to_dict() if self.proposal else None
        return d


@dataclass
class CycleResult:
    cycle_id: str
    inst_id: str
    decision: Decision
    summary: str
    reports: list[AgentReport]
    proposal: TradeProposal | None = None
    execution: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "inst_id": self.inst_id,
            "decision": self.decision.value,
            "summary": self.summary,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "execution": self.execution,
            "reports": [r.to_dict() for r in self.reports],
        }

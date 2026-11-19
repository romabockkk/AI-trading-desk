"""Shared workspace (blackboard) — the communication channel between the 8 agents.

Every agent posts an AgentReport; later agents read earlier reports before writing
their own. Each cycle is persisted to ``workspace/cycles/<cycle_id>/`` as JSON + a
human-readable Markdown transcript, so every decision is auditable.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import AgentReport, CycleResult


class Workspace:
    def __init__(self, root: Path, cycle_id: str, inst_id: str):
        self.root = root
        self.cycle_id = cycle_id
        self.inst_id = inst_id
        self.reports: list[AgentReport] = []
        self.context: dict = {}

    def post(self, report: AgentReport) -> AgentReport:
        self.reports.append(report)
        return report

    def get(self, agent: str) -> AgentReport | None:
        for r in reversed(self.reports):
            if r.agent == agent:
                return r
        return None

    def contradictions(self) -> list[str]:
        """Pairs of analysts that disagree (SUPPORT vs OPPOSE/VETO)."""
        pro = [r.agent for r in self.reports if r.stance.value == "SUPPORT"]
        con = [r.agent for r in self.reports if r.stance.value in ("OPPOSE", "VETO")]
        return [f"{a} supports while {b} opposes" for a in pro for b in con]

    # ---------------------------------------------------------------- persist
    def save(self, result: CycleResult) -> Path:
        d = self.root / "cycles" / f"{self.cycle_id}_{self.inst_id}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cycle.json").write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
        (d / "transcript.md").write_text(render_markdown(result), encoding="utf-8")
        return d


def render_markdown(result: CycleResult) -> str:
    lines = [f"# Research cycle {result.cycle_id} — {result.inst_id}", "",
             f"**Final decision: {result.decision.value}**", "", result.summary, ""]
    if result.proposal:
        p = result.proposal
        lines += ["## Trade proposal", "",
                  "| Field | Value |", "|---|---|",
                  f"| Direction | {p.direction} |", f"| Entry | {p.entry:g} |",
                  f"| Stop loss | {p.stop_loss:g} |", f"| Take profit | {p.take_profit:g} |",
                  f"| R:R | {p.rr:.2f} |",
                  f"| Size (contracts) | {p.position_size_contracts if p.position_size_contracts is not None else 'n/a'} |",
                  f"| Risk (USDT) | {p.risk_usd if p.risk_usd is not None else 'n/a'} |", ""]
    for r in result.reports:
        tag = " _(simulated role review — LLM)_" if r.simulated_role else ""
        lines += [f"## {r.agent} — {r.stance.value} (confidence {r.confidence:.0%}){tag}",
                  f"_report {r.timestamp}, data {r.data_timestamp or 'n/a'}_", ""]
        lines += [f"- {f}" for f in r.findings]
        if r.objections:
            lines += ["", "Objections:"] + [f"- **{o.severity.value}** {o.text}" for o in r.objections]
        if r.sources:
            lines += ["", "Sources: " + "; ".join(r.sources)]
        if r.recommended_action:
            lines += ["", f"Next action: {r.recommended_action}"]
        lines.append("")
    if result.execution:
        lines += ["## Execution", "", "```json", json.dumps(result.execution, indent=2, default=str), "```"]
    return "\n".join(lines)

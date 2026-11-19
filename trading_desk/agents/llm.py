"""Optional Claude review layer (off by default; enable with DESK_LLM=1).

Claude reads the whole workspace and writes an *additional* devil's-advocate style
critique. Its report is labelled ``simulated_role=True`` and it can only ADD
objections — it can never approve a trade or override the Risk Manager.
Requires the ``anthropic`` package and an API key / ``ant auth login`` credential.
"""
from __future__ import annotations

import json

from ..models import AgentReport, Objection, Severity, Stance
from ..workspace import Workspace

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "objections"],
    "properties": {
        "summary": {"type": "string"},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "severity"],
                "properties": {
                    "text": {"type": "string"},
                    "severity": {"type": "string", "enum": ["INFO", "MINOR", "MAJOR", "CRITICAL"]},
                },
            },
        },
    },
}

SYSTEM = (
    "You are the independent reviewer on a crypto demo-trading desk. You receive the structured reports "
    "of the other agents for one research cycle. Find weaknesses, contradictions and missing evidence in "
    "the proposed trade. Use only the data in the reports; do not invent prices, news or statistics. "
    "Mark an objection CRITICAL only if it clearly breaks a stated rule or makes the trade unsafe."
)


class ClaudeReviewer:
    name = "Claude Reviewer"

    def __init__(self, model: str = "claude-opus-5-5"):
        self.model = model

    def run(self, ws: Workspace) -> AgentReport:
        try:
            import anthropic
        except ImportError:
            return self._unavailable(ws, "package `anthropic` not installed")
        payload = json.dumps([r.to_dict() for r in ws.reports], default=str)
        try:
            client = anthropic.Anthropic()
            resp = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": f"Cycle reports (JSON):\n{payload}"}],
            )
        except Exception as exc:  # noqa: BLE001 - any API failure => reviewer unavailable, desk continues
            return self._unavailable(ws, f"API call failed: {type(exc).__name__}")
        if resp.stop_reason == "refusal":
            return self._unavailable(ws, "model declined the request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            out = json.loads(text)
        except ValueError:
            return self._unavailable(ws, "response was not valid JSON")
        objections = [Objection(o["text"], Severity(o["severity"])) for o in out.get("objections", [])]
        serious = any(o.severity in (Severity.MAJOR, Severity.CRITICAL) for o in objections)
        return ws.post(AgentReport(self.name, ws.inst_id, Stance.OPPOSE if serious else Stance.NEUTRAL, 0.5,
                                   [out.get("summary", "")], sources=[f"Anthropic API ({resp.model})"],
                                   objections=objections, simulated_role=True))

    def _unavailable(self, ws: Workspace, why: str) -> AgentReport:
        return ws.post(AgentReport(self.name, ws.inst_id, Stance.UNAVAILABLE, 0.0, [f"LLM review unavailable: {why}"],
                                   simulated_role=True))

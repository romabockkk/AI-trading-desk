"""Reflection memory (TradingAgents decision log + FinMem-style layered recall).

After a trade closes, the Trade Auditor writes a lesson. Before a new decision the
Head Trader and Strategy Researcher recall:
* short-term: the most recent lessons for the same instrument,
* long-term: aggregate statistics per setup type across all instruments.
Lessons inform reports only — they never change rules automatically
(AGENT_WORKFLOW.md §6: never change strategy rules based on one trade).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import iso, utcnow


class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add_lesson(self, lesson: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso(utcnow()), **lesson}, default=str) + "\n")

    def all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def recall(self, inst_id: str, setup_type: str | None, k: int = 5) -> dict[str, Any]:
        lessons = self.all()
        recent = [l for l in lessons if l.get("inst_id") == inst_id][-k:]
        same_setup = [l for l in lessons if setup_type and l.get("setup_type") == setup_type]
        r = [l["result_r"] for l in same_setup if l.get("result_r") is not None]
        return {
            "recent_same_instrument": recent,
            "setup_stats": {
                "setup_type": setup_type,
                "trades": len(r),
                "win_rate": (sum(1 for x in r if x > 0) / len(r)) if r else None,
                "avg_r": (sum(r) / len(r)) if r else None,
            },
        }

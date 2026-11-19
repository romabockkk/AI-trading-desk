"""Append-only, hash-chained trade journal (TRADE_JOURNAL.md).

Every entry stores the SHA-256 of the previous entry, so any edit or deletion of a
historical record (e.g. hiding a loss) is detected by ``verify()``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .models import iso, utcnow

GENESIS = "0" * 64


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def _last_hash(self) -> str:
        last = GENESIS
        for e in self.entries():
            last = e["hash"]
        return last

    @staticmethod
    def _digest(body: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def append(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        body = {"ts": iso(utcnow()), "kind": kind, "data": data, "prev": self._last_hash()}
        entry = {**body, "hash": self._digest(body)}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for n, e in enumerate(self.entries(), 1):
            body = {k: e[k] for k in ("ts", "kind", "data", "prev")}
            if e["prev"] != prev or self._digest(body) != e["hash"]:
                return False, f"chain broken at entry {n}"
            prev = e["hash"]
        return True, "journal intact"

    # ------------------------------------------------------------ queries
    def trades(self) -> dict[str, dict[str, Any]]:
        """Fold events into the latest state of each trade (keyed by trade_id)."""
        out: dict[str, dict[str, Any]] = {}
        for e in self.entries():
            tid = e["data"].get("trade_id")
            if not tid or not e["kind"].startswith("trade_"):
                continue
            out.setdefault(tid, {}).update(e["data"])
            out[tid]["status"] = e["kind"].removeprefix("trade_")
            out[tid]["updated"] = e["ts"]
        return out

    def closed_trades(self) -> list[dict[str, Any]]:
        return sorted((t for t in self.trades().values() if t["status"] == "closed"),
                      key=lambda t: t.get("closed_at") or t["updated"])

    def open_trades(self) -> list[dict[str, Any]]:
        return [t for t in self.trades().values() if t["status"] in ("submitted", "open")]

"""Kill switch and per-bot pause/disable state (JSON file shared by all bots)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Control:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "control.json"

    # --- persistence -------------------------------------------------------
    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data.setdefault("global_stop", False)
        data.setdefault("paused_bots", {})     # bot -> reason
        data.setdefault("disabled_bots", {})   # bot -> reason (set by cleanup --apply)
        data.setdefault("history", [])
        return data

    def save(self, data: dict) -> None:
        data["history"] = data.get("history", [])[-200:]
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".control-")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _event(self, data: dict, action: str, target: str, reason: str) -> None:
        data["history"].append({"ts": time.time(), "action": action, "target": target, "reason": reason})

    # --- commands ----------------------------------------------------------
    def stop_all(self, reason: str = "manual stop") -> None:
        data = self.load()
        data["global_stop"] = True
        data["stopped_at"] = time.time()
        data["stop_reason"] = reason
        self._event(data, "stop", "*", reason)
        self.save(data)

    def resume_all(self) -> None:
        data = self.load()
        data["global_stop"] = False
        data.pop("stop_reason", None)
        self._event(data, "resume", "*", "")
        self.save(data)

    def pause(self, bot: str, reason: str = "manual pause") -> None:
        data = self.load()
        data["paused_bots"][bot] = reason
        self._event(data, "pause", bot, reason)
        self.save(data)

    def unpause(self, bot: str) -> None:
        data = self.load()
        data["paused_bots"].pop(bot, None)
        data["disabled_bots"].pop(bot, None)
        self._event(data, "unpause", bot, "")
        self.save(data)

    def disable(self, bot: str, reason: str) -> None:
        data = self.load()
        data["disabled_bots"][bot] = reason
        self._event(data, "disable", bot, reason)
        self.save(data)

    # --- queries -----------------------------------------------------------
    def blocked_reason(self, bot: str) -> str | None:
        data = self.load()
        if data["global_stop"]:
            return f"вся команда остановлена: {data.get('stop_reason', '')}"
        if bot in data["disabled_bots"]:
            return f"бот отключён: {data['disabled_bots'][bot]}"
        if bot in data["paused_bots"]:
            return f"бот на паузе: {data['paused_bots'][bot]}"
        return None

    def is_stopped(self) -> bool:
        return bool(self.load()["global_stop"])

    def inactive_bots(self) -> set[str]:
        data = self.load()
        return set(data["paused_bots"]) | set(data["disabled_bots"])

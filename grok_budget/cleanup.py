"""Remove what costs money/space and brings nothing: dry-run by default."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .analyzer import Analysis
from .config import Config
from .control import Control
from .storage import DAY, Storage


@dataclass
class CleanupPlan:
    disable_bots: list[tuple[str, str]] = field(default_factory=list)   # (bot, reason)
    old_usage_rows: int = 0
    expired_cache: int = 0
    usage_cutoff: float = 0.0
    cache_cutoff: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.disable_bots or self.old_usage_rows or self.expired_cache)


def plan_cleanup(cfg: Config, an: Analysis, storage: Storage, control: Control) -> CleanupPlan:
    now = time.time()
    plan = CleanupPlan(
        usage_cutoff=now - cfg.retention_days * DAY,
        cache_cutoff=now - max(cfg.dedupe_window_minutes * 60, 3600),
    )
    already = control.load()["disabled_bots"]
    for s in an.bots.values():
        bot = cfg.bots.get(s.bot)
        if s.verdict == "DISABLE" and s.bot not in already and not (bot and bot.protected):
            plan.disable_bots.append((s.bot, "; ".join(s.reasons)))
    plan.old_usage_rows = storage.count_older(plan.usage_cutoff)
    plan.expired_cache = storage.count_cache_older(plan.cache_cutoff)
    return plan


def apply_cleanup(plan: CleanupPlan, storage: Storage, control: Control) -> None:
    for bot, reason in plan.disable_bots:
        control.disable(bot, f"auto-cleanup: {reason}")
    if plan.old_usage_rows:
        storage.archive_and_delete_older(plan.usage_cutoff)  # totals are kept in weekly_summary
    if plan.expired_cache:
        storage.delete_cache_older(plan.cache_cutoff)
    storage.vacuum()

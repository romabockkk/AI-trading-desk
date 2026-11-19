"""Account-level protections (RISK_MANAGEMENT.md §4, §7) — modelled on Freqtrade's
StoplossGuard / MaxDrawdown / CooldownPeriod plugins plus a manual kill switch.

Works on plain lists of closed-trade dicts so the backtester and the live desk use
identical logic. A closed trade needs: ``closed_at`` (ISO str or datetime),
``pnl_usd`` and ``equity_before``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import RiskLimits


def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


@dataclass
class ProtectionStatus:
    blocked: bool
    reasons: list[str]


def check_protections(closed: list[dict], now: datetime, limits: RiskLimits,
                      kill_switch: Path | None = None, day_start_equity: float | None = None) -> ProtectionStatus:
    reasons: list[str] = []
    if kill_switch is not None and kill_switch.exists():
        reasons.append(f"KILL SWITCH active ({kill_switch}) — remove the file to resume after review")
    closed = [t for t in closed if t.get("closed_at") and t.get("pnl_usd") is not None]
    today = [t for t in closed if _dt(t["closed_at"]).date() == now.date()]
    # StoplossGuard: consecutive losses today
    streak = 0
    for t in reversed(today):
        if t["pnl_usd"] < 0:
            streak += 1
        else:
            break
    if streak >= limits.max_consecutive_losses:
        reasons.append(f"StoplossGuard: {streak} consecutive losses today (limit {limits.max_consecutive_losses})")
    # MaxDrawdown: daily equity drawdown
    if today:
        start = day_start_equity or today[0].get("equity_before") or 0
        pnl = sum(t["pnl_usd"] for t in today)
        dd_pct = -pnl / start * 100 if start else 0.0
        if dd_pct >= limits.max_daily_drawdown_pct:
            reasons.append(f"MaxDrawdown: daily drawdown {dd_pct:.2f}% (limit {limits.max_daily_drawdown_pct}%)")
    # CooldownPeriod
    if closed:
        last = _dt(closed[-1]["closed_at"])
        until = last + timedelta(minutes=limits.cooldown_minutes_after_close)
        if now < until:
            reasons.append(f"Cooldown until {until.isoformat(timespec='minutes')} after last closed trade")
    return ProtectionStatus(bool(reasons), reasons)

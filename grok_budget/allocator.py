"""Weekly budget allocation per bot and team.

Algorithm
---------
1. distributable = weekly_budget * (1 - reserve%)   — the reserve is never handed out.
2. For each active bot:
       base   = priority / sum(priorities)                (what the bot *should* get)
       demand = last-week cost share (base if no history)  (what it *actually* needs)
       weight = (0.5*base + 0.5*demand) * efficiency       (waste lowers the weight)
   DISABLE verdict -> 0. Paused/disabled bots -> 0.
3. Normalise, then clamp to [min_share, max_share] with water-filling so the
   clamped remainder is redistributed among the free bots.
4. Each bot's daily cap = weekly allocation / plan_days (8 by default), so the
   weekly money stretches over a week *plus* a buffer day.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .analyzer import Analysis
from .config import Config


@dataclass
class Allocation:
    bot: str
    team: str
    share_percent: float
    weekly_usd: float
    daily_cap_usd: float
    reason: str


def allocate(cfg: Config, analysis: Analysis, inactive: set[str] | None = None) -> dict[str, Allocation]:
    inactive = inactive or set()
    active = [
        b for b in cfg.bots.values()
        if b.name not in inactive and analysis.bots.get(b.name) is not None
        and analysis.bots[b.name].verdict != "DISABLE"
    ]
    result: dict[str, Allocation] = {}
    for b in cfg.bots.values():
        if b not in active:
            why = "пауза/отключён" if b.name in inactive else "вердикт DISABLE"
            result[b.name] = Allocation(b.name, b.team, 0.0, 0.0, 0.0, why)
    if not active:
        return result

    prio_sum = sum(b.priority for b in active) or 1
    cost_sum = sum(analysis.bots[b.name].cost_usd for b in active)
    weights: dict[str, float] = {}
    for b in active:
        st = analysis.bots[b.name]
        base = b.priority / prio_sum
        demand = st.cost_usd / cost_sum if cost_sum > 0 else base
        weights[b.name] = (0.5 * base + 0.5 * demand) * st.efficiency

    shares = _clamp_shares(weights, {b.name: (b.min_share, b.max_share) for b in active})
    pool = cfg.distributable_usd
    for b in active:
        st = analysis.bots[b.name]
        share = shares[b.name]
        weekly = pool * share / 100
        reason = f"приоритет {b.priority}, эффективность {st.efficiency:.0%}"
        if st.verdict == "REDUCE":
            reason += ", урезан из-за потерь"
        result[b.name] = Allocation(b.name, b.team, share, weekly, weekly / cfg.plan_days, reason)
    return result


def _clamp_shares(weights: dict[str, float], bounds: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Proportional split of 100% with per-item [min, max] bounds (water-filling)."""
    fixed: dict[str, float] = {}
    free = dict(weights)
    for _ in range(len(weights) + 1):
        remaining = 100 - sum(fixed.values())
        wsum = sum(free.values())
        if not free:
            break
        tentative = {
            k: (remaining * w / wsum if wsum > 0 else remaining / len(free)) for k, w in free.items()
        }
        violated = False
        for k, v in tentative.items():
            lo, hi = bounds[k]
            if v < lo:
                fixed[k] = lo
                violated = True
            elif v > hi:
                fixed[k] = hi
                violated = True
        for k in fixed:
            free.pop(k, None)
        if not violated:
            fixed.update(tentative)
            break
    # if every bot hit its max (sum < 100) the leftover simply stays unallocated (extra reserve)
    return fixed


def team_totals(allocs: dict[str, Allocation]) -> dict[str, float]:
    out: dict[str, float] = {}
    for a in allocs.values():
        out[a.team] = out.get(a.team, 0.0) + a.weekly_usd
    return out


def save_allocations(state_dir: Path, cfg: Config, allocs: dict[str, Allocation]) -> Path:
    path = state_dir / "allocations.json"
    payload = {
        "generated_at": time.time(),
        "weekly_budget_usd": cfg.weekly_budget_usd,
        "reserve_usd": cfg.reserve_usd,
        "distributable_usd": cfg.distributable_usd,
        "plan_days": cfg.plan_days,
        "bots": {k: asdict(v) for k, v in allocs.items()},
        "teams": team_totals(allocs),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def load_allocations(state_dir: Path) -> dict | None:
    try:
        return json.loads((state_dir / "allocations.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None

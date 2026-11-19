"""Usage analysis: who spends what, and which spending brings nothing useful."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import Config
from .storage import WEEK, Storage, UsageRow

WASTE_STATUSES = {"error", "empty"}


@dataclass
class BotStats:
    bot: str
    label: str
    team: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    empty: int = 0
    duplicates: int = 0            # identical prompt already sent earlier in window (paid twice)
    duplicate_cost: float = 0.0
    cache_hits: int = 0
    blocked: int = 0
    useful: int = 0
    useless: int = 0
    useless_cost: float = 0.0
    waste_cost: float = 0.0
    last_ts: float | None = None
    share_percent: float = 0.0
    models: Counter = field(default_factory=Counter)
    verdict: str = "OK"
    reasons: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def paid_calls(self) -> int:
        return self.calls - self.cache_hits - self.blocked

    @property
    def waste_percent(self) -> float:
        return 100 * self.waste_cost / self.cost_usd if self.cost_usd > 0 else 0.0

    @property
    def efficiency(self) -> float:
        """1.0 = all spending useful; floor 0.2 so a bot is never starved purely by stats."""
        return max(0.2, 1 - self.waste_percent / 100)

    @property
    def avg_prompt_tokens(self) -> float:
        return self.prompt_tokens / self.paid_calls if self.paid_calls else 0.0


@dataclass
class Analysis:
    since: float
    until: float
    bots: dict[str, BotStats]
    total_cost: float
    total_tokens: int
    total_waste: float
    team_cost: dict[str, float]
    daily_cost: dict[str, dict[str, float]]   # "YYYY-MM-DD" -> team -> cost

    @property
    def waste_percent(self) -> float:
        return 100 * self.total_waste / self.total_cost if self.total_cost else 0.0


def analyze(cfg: Config, storage: Storage, since: float | None = None, until: float | None = None) -> Analysis:
    until = until or time.time()
    since = since if since is not None else until - WEEK
    rows = storage.rows(since, until)
    return analyze_rows(cfg, rows, since, until)


def analyze_rows(cfg: Config, rows: list[UsageRow], since: float, until: float) -> Analysis:
    stats: dict[str, BotStats] = {
        name: BotStats(bot=name, label=b.label, team=b.team) for name, b in cfg.bots.items()
    }
    seen_hashes: dict[str, set[str]] = defaultdict(set)
    team_cost: dict[str, float] = defaultdict(float)
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for r in rows:
        s = stats.get(r.bot)
        if s is None:  # bot not in config (e.g. removed) — still count it
            s = stats[r.bot] = BotStats(bot=r.bot, label=r.bot, team=r.team)
        s.calls += 1
        s.prompt_tokens += r.prompt_tokens
        s.completion_tokens += r.completion_tokens
        s.cost_usd += r.cost_usd
        s.models[r.model] += 1
        s.last_ts = r.ts
        team_cost[s.team] += r.cost_usd
        daily[time.strftime("%Y-%m-%d", time.localtime(r.ts))][s.team] += r.cost_usd

        wasted = False
        if r.status == "error":
            s.errors += 1
            wasted = True
        elif r.status == "empty":
            s.empty += 1
            wasted = True
        elif r.status == "cache_hit":
            s.cache_hits += 1
        elif r.status == "blocked":
            s.blocked += 1
        if r.prompt_hash and r.status not in ("cache_hit", "blocked"):
            if r.prompt_hash in seen_hashes[r.bot]:
                s.duplicates += 1
                s.duplicate_cost += r.cost_usd
                wasted = True
            seen_hashes[r.bot].add(r.prompt_hash)
        if r.outcome == "useful":
            s.useful += 1
        elif r.outcome == "useless":
            s.useless += 1
            s.useless_cost += r.cost_usd
            wasted = True
        if wasted:
            s.waste_cost += r.cost_usd

    total_cost = sum(s.cost_usd for s in stats.values())
    for s in stats.values():
        s.share_percent = 100 * s.cost_usd / total_cost if total_cost else 0.0
        _verdict(cfg, s, until)

    return Analysis(
        since=since,
        until=until,
        bots=stats,
        total_cost=total_cost,
        total_tokens=sum(s.tokens for s in stats.values()),
        total_waste=sum(s.waste_cost for s in stats.values()),
        team_cost=dict(team_cost),
        daily_cost={d: dict(v) for d, v in sorted(daily.items())},
    )


# verdict priority: DISABLE > REDUCE > CACHE > TRIM > IDLE > OK
def _verdict(cfg: Config, s: BotStats, now: float) -> None:
    a = cfg.analysis
    bot = cfg.bots.get(s.bot)
    verdicts: list[str] = []

    if bot is None:
        verdicts.append("DISABLE")
        s.reasons.append("бота нет в конфигурации, но он тратит бюджет")

    if s.calls == 0:
        verdicts.append("IDLE")
        s.reasons.append("за период не было вызовов — бюджет можно отдать другим")
    elif s.calls >= a["min_calls_for_verdict"]:
        no_value = s.useful == 0
        if s.waste_percent >= a["waste_disable_percent"] and no_value:
            if bot and bot.protected:
                verdicts.append("REDUCE")
                s.reasons.append(f"{s.waste_percent:.0f}% потерь, но бот защищён (protected) — только урезаем")
            else:
                verdicts.append("DISABLE")
                s.reasons.append(f"{s.waste_percent:.0f}% потерь и ни одного полезного ответа")
        elif s.waste_percent >= a["waste_reduce_percent"]:
            verdicts.append("REDUCE")
            s.reasons.append(f"{s.waste_percent:.0f}% бюджета ушло на ошибки/пустые/повторные/бесполезные ответы")

        paid = max(1, s.paid_calls)
        if 100 * s.duplicates / paid >= a["duplicate_cache_percent"]:
            verdicts.append("CACHE")
            s.reasons.append(f"{s.duplicates} повторных одинаковых запросов — включить кэш / увеличить окно")
        if s.avg_prompt_tokens >= a["long_prompt_tokens"]:
            verdicts.append("TRIM")
            s.reasons.append(f"средний prompt {s.avg_prompt_tokens:,.0f} токенов — сократить контекст/историю")
        if s.errors and 100 * s.errors / paid >= 10:
            s.reasons.append(f"{s.errors} ошибок API — проверить запросы/ретраи")

    if bot and s.share_percent > bot.max_share and s.calls:
        if "REDUCE" not in verdicts:
            verdicts.append("REDUCE")
        s.reasons.append(f"забирает {s.share_percent:.1f}% при лимите {bot.max_share:.0f}%")

    for v in ("DISABLE", "REDUCE", "CACHE", "TRIM", "IDLE"):
        if v in verdicts:
            s.verdict = v
            break

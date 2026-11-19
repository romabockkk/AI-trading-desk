"""Budget-aware Grok (xAI) client. Every bot must call Grok ONLY through this class:
that is what makes the kill switch, the limits and the usage dashboard work."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .allocator import load_allocations
from .config import Config, load_config
from .control import Control
from .storage import DAY, WEEK, Storage


class BotStopped(RuntimeError):
    """Raised when the team / bot is stopped, paused or disabled."""


class BudgetExceeded(RuntimeError):
    """Raised (hard enforcement) when a bot has used up its weekly or daily limit."""


class GrokClient:
    def __init__(self, bot: str, cfg: Config | None = None, transport=None):
        self.cfg = cfg or load_config()
        if bot not in self.cfg.bots:
            raise ValueError(f"unknown bot '{bot}' — add it to grok_config.toml")
        self.bot = bot
        self.team = self.cfg.bots[bot].team
        self.storage = Storage(self.cfg.state_dir)
        self.control = Control(self.cfg.state_dir)
        self._transport = transport or self._http_post  # injectable for tests
        self.last_call_id: int | None = None
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ API
    def should_stop(self) -> bool:
        """Bots should check this in their main loop and exit gracefully."""
        return self.control.blocked_reason(self.bot) is not None

    def chat(self, messages: list[dict[str, Any]], model: str | None = None,
             max_tokens: int | None = None, use_cache: bool = True, **extra: Any) -> str:
        model = model or self.cfg.model_for(self.bot)
        reason = self.control.blocked_reason(self.bot)
        if reason:
            self._log_blocked(model)
            raise BotStopped(f"{self.bot}: {reason}")
        self._check_budget(model)

        cap = self.cfg.bots[self.bot].max_output_tokens
        if cap:
            max_tokens = min(max_tokens or cap, cap)

        prompt_hash = _hash(model, messages, max_tokens, extra)
        window = self.cfg.dedupe_window_minutes * 60
        if use_cache and window > 0:
            cached = self.storage.cache_get(prompt_hash, window)
            if cached is not None:
                self.last_call_id = self.storage.log(
                    bot=self.bot, team=self.team, model=model, status="cache_hit", prompt_hash=prompt_hash)
                return cached

        body = {"model": model, "messages": messages, **extra}
        if max_tokens:
            body["max_tokens"] = max_tokens
        t0 = time.time()
        try:
            resp = self._transport(body)
        except Exception:
            self.last_call_id = self.storage.log(
                bot=self.bot, team=self.team, model=model, status="error", prompt_hash=prompt_hash,
                latency_ms=int((time.time() - t0) * 1000))
            raise
        latency = int((time.time() - t0) * 1000)
        usage = resp.get("usage") or {}
        pt = int(usage.get("prompt_tokens", 0))
        ct = int(usage.get("completion_tokens", 0))
        content = ""
        choices = resp.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        status = "ok" if content.strip() else "empty"
        self.last_call_id = self.storage.log(
            bot=self.bot, team=self.team, model=model, prompt_tokens=pt, completion_tokens=ct,
            cost_usd=self.cfg.cost(model, pt, ct), status=status, prompt_hash=prompt_hash, latency_ms=latency)
        if status == "ok" and use_cache and window > 0:
            self.storage.cache_put(prompt_hash, content)
        return content

    def mark(self, useful: bool, call_id: int | None = None) -> None:
        """Tag the last answer as useful / useless. Feeds the waste analysis."""
        cid = call_id or self.last_call_id
        if cid:
            self.storage.set_outcome(cid, "useful" if useful else "useless")

    def remaining(self) -> dict[str, float | None]:
        lim = self._limits()
        now = time.time()
        week_spent = self.storage.spent(now - WEEK, self.bot)
        day_spent = self.storage.spent(now - DAY, self.bot)
        return {
            "weekly_limit": lim[0], "weekly_left": None if lim[0] is None else lim[0] - week_spent,
            "daily_cap": lim[1], "daily_left": None if lim[1] is None else lim[1] - day_spent,
        }

    # ------------------------------------------------------------ internals
    def _limits(self) -> tuple[float | None, float | None]:
        allocs = load_allocations(self.cfg.state_dir)
        if not allocs or self.bot not in allocs.get("bots", {}):
            return None, None
        a = allocs["bots"][self.bot]
        return float(a["weekly_usd"]), float(a["daily_cap_usd"])

    def _check_budget(self, model: str) -> None:
        now = time.time()
        problems = []
        # global guard: never exceed the distributable part of the weekly budget
        if self.storage.spent(now - WEEK) >= self.cfg.distributable_usd:
            problems.append("общий недельный бюджет (без резерва) исчерпан")
        weekly, daily = self._limits()
        if weekly is not None and self.storage.spent(now - WEEK, self.bot) >= weekly:
            problems.append(f"недельный лимит ${weekly:.2f} исчерпан")
        if daily is not None and self.storage.spent(now - DAY, self.bot) >= daily:
            problems.append(f"дневной лимит ${daily:.2f} исчерпан (бюджет растянут на {self.cfg.plan_days:g} дн.)")
        if not problems:
            return
        msg = f"{self.bot}: " + "; ".join(problems)
        if self.cfg.enforcement == "hard":
            self._log_blocked(model)
            raise BudgetExceeded(msg)
        self.warnings.append(msg)

    def _log_blocked(self, model: str) -> None:
        self.storage.log(bot=self.bot, team=self.team, model=model, status="blocked")

    def _http_post(self, body: dict) -> dict:
        key = os.environ.get(self.cfg.api_key_env)
        if not key:
            raise RuntimeError(f"переменная окружения {self.cfg.api_key_env} не задана")
        req = urllib.request.Request(
            f"{self.cfg.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:  # never include the key in errors
            raise RuntimeError(f"xAI API HTTP {e.code}: {e.read()[:300]!r}") from None


def _hash(model: str, messages: list, max_tokens: int | None, extra: dict) -> str:
    blob = json.dumps([model, messages, max_tokens, extra], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()

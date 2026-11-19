"""Agent runtime: config, timeout, retries, strict output validation, failure handling."""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..config import PROJECT_ROOT
from ..schemas.agents import CRITICAL_AGENTS, PAYLOAD_MODELS, AgentName, AgentReport, ReportStatus, SourceRef
from ..schemas.common import utcnow
from .llm import LLMClient, LLMError

PROMPTS_DIR = Path(__file__).parent / "prompts"
_POOL = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent")


@dataclass(frozen=True)
class AgentConfig:
    name: AgentName
    critical: bool
    timeout_s: float
    retries: int
    max_tokens: int
    llm_review: bool
    tools: tuple[str, ...]
    system_prompt: str


def load_agent_configs(path: str | Path = "config/agents.toml") -> dict[AgentName, AgentConfig]:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    data = tomllib.loads(p.read_text())
    d = data.get("defaults", {})
    out = {}
    for name in AgentName:
        a = {**d, **data.get("agents", {}).get(name.value, {})}
        crit = bool(a.get("critical", False))
        if name in CRITICAL_AGENTS and not crit:
            raise ValueError(f"{name.value} is a critical agent by design and cannot be configured non-critical")
        out[name] = AgentConfig(name, crit, float(a["timeout_s"]), int(a["retries"]), int(a["max_tokens"]),
                                bool(a.get("llm_review", False)), tuple(a.get("tools", [])),
                                (PROMPTS_DIR / f"{name.value}.md").read_text())
    return out


@dataclass
class AgentOutput:
    payload: dict
    confidence: float | None = None
    sources: list[SourceRef] = field(default_factory=list)
    snapshot_ids: list[str] = field(default_factory=list)


class BaseAgent:
    name: AgentName

    def __init__(self, cfg: AgentConfig, llm: LLMClient | None = None):
        self.cfg = cfg
        self.llm = llm if cfg.llm_review else None

    def analyze(self, ctx) -> AgentOutput:  # implemented by subclasses; deterministic
        raise NotImplementedError

    def run(self, ctx, cycle_id: str) -> AgentReport:
        rid = f"rep-{self.name.value.lower()}-{uuid.uuid4().hex[:10]}"
        t0 = time.monotonic()
        attempts = 0
        last_err = None
        status = ReportStatus.FAILED
        for attempts in range(1, self.cfg.retries + 2):
            fut = _POOL.submit(self.analyze, ctx)
            try:
                out: AgentOutput = fut.result(timeout=self.cfg.timeout_s)
                PAYLOAD_MODELS[self.name].model_validate(out.payload)   # strict schema check
                report = AgentReport(report_id=rid, cycle_id=cycle_id, agent=self.name, created_utc=utcnow(),
                                     status=ReportStatus.OK, confidence=out.confidence, snapshot_ids=out.snapshot_ids,
                                     sources=out.sources, payload=json.loads(json.dumps(out.payload, default=str)),
                                     attempts=attempts, latency_ms=int((time.monotonic() - t0) * 1000))
                return self._with_llm(report)
            except cf.TimeoutError:
                status, last_err = ReportStatus.TIMEOUT, f"timeout after {self.cfg.timeout_s}s"
            except ValidationError as e:
                status, last_err = ReportStatus.INVALID_OUTPUT, f"schema validation failed: {e.errors()[:3]}"
            except Exception as e:  # noqa: BLE001 - any agent failure is recorded, never raised
                status, last_err = ReportStatus.FAILED, f"{type(e).__name__}: {e}"
        return AgentReport(report_id=rid, cycle_id=cycle_id, agent=self.name, created_utc=utcnow(), status=status,
                           error=last_err, attempts=attempts, latency_ms=int((time.monotonic() - t0) * 1000))

    def _with_llm(self, report: AgentReport) -> AgentReport:
        if self.llm is None:
            return report
        try:
            res = self.llm.review(self.cfg.system_prompt, report.payload or {}, self.cfg.max_tokens, self.cfg.timeout_s)
            rv = {"provider": self.llm.provider, "model": res.model, "tokens_in": res.tokens_in,
                  "tokens_out": res.tokens_out, **res.review.model_dump()}
        except LLMError as e:
            rv = {"provider": self.llm.provider, "error": str(e)}
        return report.model_copy(update={"llm_review": rv})

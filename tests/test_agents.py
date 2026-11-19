import time
from dataclasses import replace
from datetime import timedelta

import pytest

from helpers import LIMITS, T0, settings
from tradedesk.agents.base import AgentOutput, BaseAgent, load_agent_configs
from tradedesk.agents.llm import LLMResult, LLMReview, make_llm
from tradedesk.agents.team import MacroAgent, CycleContext
from tradedesk.data.service import MarketDataService
from tradedesk.schemas.agents import CRITICAL_AGENTS, AgentName, ReportStatus

CFG = load_agent_configs()


def test_all_eight_agents_configured_with_prompts():
    assert set(CFG) == set(AgentName)
    for a, c in CFG.items():
        assert c.timeout_s > 0 and c.retries >= 0 and c.max_tokens > 0 and len(c.system_prompt) > 100 and c.tools
        assert c.critical == (a in CRITICAL_AGENTS)


def test_critical_agent_cannot_be_configured_non_critical(tmp_path):
    p = tmp_path / "a.toml"
    p.write_text('[defaults]\ntimeout_s=1\nretries=0\nmax_tokens=10\n[agents.RISK_MANAGER]\ncritical=false\n')
    with pytest.raises(ValueError, match="critical"):
        load_agent_configs(p)


class Slow(BaseAgent):
    name = AgentName.QUANT

    def analyze(self, ctx):
        time.sleep(0.5)


class Bad(BaseAgent):
    name = AgentName.SKEPTIC

    def analyze(self, ctx):
        return AgentOutput({"objections": "not a list"})


class Boom(BaseAgent):
    name = AgentName.MACRO
    calls = 0

    def analyze(self, ctx):
        Boom.calls += 1
        raise RuntimeError("feed down")


def test_timeout_retry_invalid_and_failure_are_reported_not_raised():
    r = Slow(replace(CFG[AgentName.QUANT], timeout_s=0.05, retries=1)).run(None, "c")
    assert r.status is ReportStatus.TIMEOUT and r.attempts == 2
    r = Bad(CFG[AgentName.SKEPTIC]).run(None, "c")
    assert r.status is ReportStatus.INVALID_OUTPUT and "schema" in r.error
    r = Boom(replace(CFG[AgentName.MACRO], retries=2)).run(None, "c")
    assert r.status is ReportStatus.FAILED and Boom.calls == 3 and "feed down" in r.error


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, assessment="DISAGREE"):
        self.assessment = assessment

    def review(self, system_prompt, report_json, max_tokens, timeout_s):
        return LLMResult(LLMReview(assessment=self.assessment, concerns=["c1"], summary="s"), 10, 5, self.model)


def test_llm_review_attached_only_when_enabled():
    class Ok(BaseAgent):
        name = AgentName.SKEPTIC

        def analyze(self, ctx):
            return AgentOutput({"objections": [], "demand_no_trade": False, "summary": "x"})
    r = Ok(CFG[AgentName.SKEPTIC], FakeLLM()).run(None, "c")          # skeptic has llm_review=true
    assert r.llm_review["assessment"] == "DISAGREE" and r.llm_review["tokens_in"] == 10
    r2 = Ok(replace(CFG[AgentName.SKEPTIC], llm_review=False), FakeLLM()).run(None, "c")
    assert r2.llm_review is None


def test_llm_provider_none_and_unknown():
    assert make_llm("none") is None
    with pytest.raises(Exception):
        make_llm("something-else")


def _ctx(tmp_path, conn, calendar=None):
    snap = MarketDataService(history_days=15).snapshot("XAUUSD", T0 + timedelta(minutes=2))
    ctx = CycleContext(conn=conn, settings=settings(), limits=LIMITS, snapshot=snap, now=T0, active=[], research=[],
                       account=None, open_positions=[], consecutive_losses=0, day_start_equity=None,
                       calendar_path=tmp_path / "calendar.csv")
    if calendar:
        ctx.calendar_path.write_text(calendar)
    return ctx


def test_macro_unknown_without_calendar(tmp_path, conn):
    out = MacroAgent(CFG[AgentName.MACRO]).analyze(_ctx(tmp_path, conn))
    assert out.payload["blackout_active"] is None and out.payload["data_status"] == "UNKNOWN"
    assert "UNKNOWN" in out.payload["summary"]


def test_macro_blackout_from_sourced_calendar_and_ignores_unsourced(tmp_path, conn):
    cal = ("event,scheduled_utc,impact,currency,source_name,source_url,published_utc\n"
           f"US CPI,{(T0 + timedelta(minutes=10)).isoformat()},HIGH,USD,BLS,https://www.bls.gov,2026-06-01T00:00:00+00:00\n"
           f"Rumour,{(T0 + timedelta(minutes=5)).isoformat()},HIGH,USD,,,\n")
    out = MacroAgent(CFG[AgentName.MACRO]).analyze(_ctx(tmp_path, conn, cal))
    assert out.payload["blackout_active"] is True and len(out.payload["upcoming_events"]) == 1
    assert out.payload["upcoming_events"][0]["source"]["name"] == "BLS"
    cal2 = cal.replace("HIGH,USD,BLS", "LOW,USD,BLS")
    out2 = MacroAgent(CFG[AgentName.MACRO]).analyze(_ctx(tmp_path, conn, cal2))
    assert out2.payload["blackout_active"] is False

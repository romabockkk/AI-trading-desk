import json
from datetime import timedelta

import pytest

from helpers import LIMITS, T0, complete_gold_spec, market, settings
from tradedesk.agents.base import AgentOutput
from tradedesk.agents.orchestrator import Orchestrator
from tradedesk.agents.team import QuantAgent
from tradedesk.config import LoadedRiskLimits
from tradedesk.db import database as db
from tradedesk.learning import registry
from tradedesk.schemas.agents import AgentName, ReportStatus

NOW = T0 + timedelta(minutes=2)
DS = market()
# These pipeline tests use a $10,000 TEST balance so the trade path can be exercised with ATR-sized stops;
# the $100 default is covered by test_paper/test_risk_gate (where most gold trades are correctly refused).
BIG = {"tradedesk_paper_initial_balance": 10_000.0}


def orch(conn, limits=LIMITS, **skw):
    complete_gold_spec(conn)
    return Orchestrator(conn, settings(**{**BIG, **skw}), limits, DS)


def test_cycle_persists_all_reports_and_decision(conn):
    r = orch(conn).run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and r["data_source"] == "FIXTURE" and r["quote_latency_s"] is not None
    assert set(r["agent_status"]) == {a.value for a in AgentName}
    assert conn.execute("SELECT COUNT(*) FROM agent_reports WHERE cycle_id=?", (r["cycle_id"],)).fetchone()[0] == 8
    d = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (r["decision_id"],)).fetchone()
    assert d["action"] == "NO_TRADE" and json.loads(d["snapshot_ids"]) and d["risk_limits_sha256"] == LIMITS.sha256
    assert "no active" in d["no_trade_reason"]
    assert db.verify_audit_chain(conn)[0]


def _good_evidence():
    return {"oos_metrics": {"n_trades": 120, "expectancy_r": 0.4, "data_is_mock": True},
            "bootstrap": {"ci95": [0.1, 0.7]}, "positive_folds": 4, "n_folds": 4}


def _inject_signal(monkeypatch, action="LONG", evidence=None):
    real = QuantAgent.analyze

    def fake(self, ctx):
        out = real(self, ctx)
        q = ctx.snapshot.quote
        a = ctx.snapshot.atr()
        entry = q.ask if action == "LONG" else q.bid
        sl = entry - 1.5 * a if action == "LONG" else entry + 1.5 * a
        tp = entry + 3.2 * a if action == "LONG" else entry - 3.2 * a
        out.payload["signals"] = [{"strategy_id": "TREND_CONTINUATION", "version": "1.0.0", "active": True,
                                   "action": action, "entry": entry, "sl": sl, "tp": tp, "planned_rr": 2.13,
                                   "rr_target": 2.13, "reason": "test signal", "invalidation": "test",
                                   "rule_ids": ["TC.REGIME.v1"], "signal_bar_utc": NOW.isoformat(),
                                   "experiment_id": "exp-test", "oos_metrics_ref": evidence}]
        return out
    monkeypatch.setattr(QuantAgent, "analyze", fake)


def _h4_bias(conn):
    from tradedesk.analysis import structure as ms
    from tradedesk.schemas.market import Timeframe
    b = DS.snapshot(conn, "XAUUSD", NOW).bars[Timeframe.H4]
    return ms.bias(ms.analyze(b, k=2), len(b) - 1)


def test_end_to_end_paper_trade_with_strong_evidence(conn, monkeypatch):
    bias = _h4_bias(conn)
    action = "SHORT" if bias == "BEARISH" else "LONG"
    _inject_signal(monkeypatch, action, _good_evidence())
    o = orch(conn)
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["risk_gate"] == "ALLOW", (r["gate_failures"], r["reasons"])
    assert r["action"] == action and r["execution"]["venue"] == "PAPER" and r["execution"]["status"] == "FILLED"
    assert r["execution"]["reconciliation_ok"] is True
    pos = o.paper.open_positions()
    assert len(pos) == 1 and pos[0]["stop_loss"]
    # second cycle: same signal must be blocked (one position, no stacking)
    r2 = o.run_cycle("XAUUSD", NOW + timedelta(seconds=5))
    assert r2["action"] == "NO_TRADE" and len(o.paper.open_positions()) == 1
    # debate recorded, within 2 rounds
    rounds = {row[0] for row in conn.execute("SELECT round FROM debates WHERE cycle_id=?", (r["cycle_id"],))}
    assert rounds <= {1, 2}


def test_weak_evidence_blocked_by_skeptic(conn, monkeypatch):
    _inject_signal(monkeypatch, "SHORT" if _h4_bias(conn) == "BEARISH" else "LONG", None)
    o = orch(conn)
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and r["risk_gate"] == "NOT_EVALUATED"
    msgs = [row["message_type"] for row in conn.execute("SELECT message_type FROM debates WHERE cycle_id=?", (r["cycle_id"],))]
    assert "OBJECTION" in msgs and "RESPONSE" in msgs
    assert any("unresolved" in x for x in r["reasons"])


def test_owner_policy_allows_unproven_in_paper(conn, monkeypatch):
    _inject_signal(monkeypatch, "SHORT" if _h4_bias(conn) == "BEARISH" else "LONG", None)
    relaxed = LoadedRiskLimits(limits=LIMITS.limits.model_copy(update={"paper_allow_unproven_strategies": True}),
                               source_path="x", sha256=LIMITS.sha256)
    o = orch(conn, relaxed)
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["risk_gate"] == "ALLOW", r


def test_critical_agent_failure_forces_no_trade(conn, monkeypatch):
    _inject_signal(monkeypatch, "SHORT" if _h4_bias(conn) == "BEARISH" else "LONG", _good_evidence())
    from tradedesk.agents.team import MacroAgent
    monkeypatch.setattr(MacroAgent, "analyze", lambda self, ctx: (_ for _ in ()).throw(RuntimeError("calendar down")))
    o = orch(conn)
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and r["agent_status"]["MACRO"] == "FAILED"
    assert any("critical agent failure" in x or "Macro report missing" in json.dumps(r) for x in r["reasons"]) or \
        r["risk_gate"] in ("DENY", "NOT_EVALUATED")
    assert conn.execute("SELECT COUNT(*) FROM errors WHERE component='agent:MACRO'").fetchone()[0] == 1


def test_kill_switch_blocks_execution(conn, monkeypatch):
    _inject_signal(monkeypatch, "SHORT" if _h4_bias(conn) == "BEARISH" else "LONG", _good_evidence())
    o = orch(conn)
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    db.set_state(conn, "kill_switch", "ON", "OWNER")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and any("KILL_SWITCH_OFF" in f for f in r["gate_failures"])
    assert o.paper.open_positions() == []


def test_llm_disagreement_can_only_downgrade(conn, monkeypatch):
    from test_agents import FakeLLM
    _inject_signal(monkeypatch, "SHORT" if _h4_bias(conn) == "BEARISH" else "LONG", _good_evidence())
    complete_gold_spec(conn)
    o = Orchestrator(conn, settings(**BIG), LIMITS, DS, llm=FakeLLM("DISAGREE"))
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE"
    o2 = Orchestrator(conn, settings(**BIG), LIMITS, DS, llm=FakeLLM("AGREE"))
    r2 = o2.run_cycle("XAUUSD", NOW + timedelta(seconds=1))
    assert r2["risk_gate"] == "ALLOW"


def test_mock_data_never_trades(conn, monkeypatch):
    from tradedesk.marketdata.providers import MockProvider
    _inject_signal(monkeypatch, "LONG", _good_evidence())
    complete_gold_spec(conn)
    o = Orchestrator(conn, settings(**BIG), LIMITS, market(MockProvider()))
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and r["data_is_mock"] is True
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_incomplete_spec_never_trades(conn, monkeypatch):
    _inject_signal(monkeypatch, "LONG", _good_evidence())
    o = Orchestrator(conn, settings(**BIG), LIMITS, DS)       # spec NOT completed
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert r["action"] == "NO_TRADE" and "volume_min" in r["spec_missing"]


def test_default_100_usd_account_refuses_atr_sized_gold_trade(conn, monkeypatch):
    bias = _h4_bias(conn)
    _inject_signal(monkeypatch, "SHORT" if bias == "BEARISH" else "LONG", _good_evidence())
    complete_gold_spec(conn)
    o = Orchestrator(conn, settings(), LIMITS, DS)            # default: $100
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", NOW)
    assert o.paper.balance() == 100.0
    assert r["action"] == "NO_TRADE" and r["risk_gate"] == "DENY"
    assert any(f.startswith("MIN_VOLUME_WITHIN_RISK") for f in r["gate_failures"])

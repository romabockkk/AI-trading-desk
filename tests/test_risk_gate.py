from dataclasses import replace

import pytest

from helpers import LIMITS, T0, account, quote
from tradedesk.config import LoadedRiskLimits, Mode, RiskLimits
from tradedesk.data.instruments import mock_spec
from tradedesk.risk import sizing
from tradedesk.risk.gate import GateInput, OpenPositionRisk, evaluate
from tradedesk.schemas.trading import Action, TradeProposal

SPEC = mock_spec("XAUUSD")


def prop(action=Action.LONG, entry=2650.25, sl=2645.0, tp=2661.0, did="d1"):
    return TradeProposal(decision_id=did, cycle_id="c", symbol="XAUUSD", action=action, entry=entry, stop_loss=sl,
                         take_profit=tp, created_utc=T0)


def base(**kw):
    d = dict(mode=Mode.PAPER, proposal=prop(), spec=SPEC, quote=quote(), account=account(), now_utc=T0,
             day_start_equity=10_000.0, consecutive_losses=0, open_positions=(), kill_switch_on=False, paused=False,
             market_open=True, data_valid=True, critical_failures=(), risk_manager_approved=True,
             skeptic_demands_no_trade=False, news_blackout=False, atr=3.0)
    d.update(kw)
    return GateInput(**d)


def failed(gi, limits=LIMITS):
    return {c.name for c in evaluate(gi, limits).failed}


def test_allow_when_everything_ok():
    g = evaluate(base(), LIMITS)
    assert g.result.value == "ALLOW", [c for c in g.failed]
    assert g.volume_lots == pytest.approx(0.09)        # $50 / ($5.25 + $0.25 spread) per 1.0 move * 100oz
    assert g.risk_money <= 50.0
    assert g.risk_limits_sha256 == LIMITS.sha256


def test_sizing_math_xau():
    lots, rm = sizing.size_position(SPEC, 10_000, 0.5, 2650.0, 2645.0)
    assert lots == 0.1 and rm == pytest.approx(50.0)
    assert sizing.round_volume_down(SPEC, 0.0999) == 0.09


@pytest.mark.parametrize("kw,name", [
    ({"kill_switch_on": True}, "KILL_SWITCH_OFF"),
    ({"paused": True}, "NOT_PAUSED"),
    ({"market_open": False}, "MARKET_OPEN"),
    ({"critical_failures": ("MACRO",)}, "CRITICAL_AGENTS_OK"),
    ({"risk_manager_approved": False}, "RISK_MANAGER_APPROVED"),
    ({"skeptic_demands_no_trade": True}, "SKEPTIC_NO_DEMAND"),
    ({"data_valid": False}, "DATA_VALID"),
    ({"spec": None}, "SPEC_AVAILABLE"),
    ({"quote": None}, "QUOTE_FRESH"),
    ({"quote": quote(t=T0.replace(minute=0, second=0).replace(hour=9))}, "QUOTE_FRESH"),
    ({"quote": quote(bid=2650.0, ask=2651.0)}, "SPREAD_LIMIT"),
    ({"account": None}, "ACCOUNT_KNOWN"),
    ({"proposal": prop(tp=2658.0)}, "REWARD_RISK"),
    ({"proposal": prop(sl=2650.10, tp=2651.0)}, "SPREAD_TO_STOP_RATIO"),
    ({"proposal": prop(sl=2649.0, tp=2700)}, "EFFECTIVE_LEVERAGE"),
    ({"proposal": prop(entry=2660.0, sl=2655.0, tp=2680)}, "ENTRY_NEAR_QUOTE"),
    ({"open_positions": (OpenPositionRisk("XAGUSD", "BUY", 40.0, "x"),)}, "MAX_CONCURRENT_POSITIONS"),
    ({"open_positions": (OpenPositionRisk("XAUUSD", "BUY", 10.0, "x"),)}, "NO_AVERAGING_OR_STACKING"),
    ({"already_ordered_decision_ids": frozenset({"d1"})}, "NO_DUPLICATE_ORDER"),
    ({"day_start_equity": 10_300.0}, "DAILY_DRAWDOWN"),
    ({"day_start_equity": None}, "DAILY_DRAWDOWN"),
    ({"consecutive_losses": 2}, "LOSS_STREAK"),
    ({"news_blackout": True}, "NEWS"),
    ({"mode": Mode.RESEARCH}, "MODE_ALLOWS_EXECUTION"),
    ({"account": account(margin=950.0)}, "MARGIN_USAGE"),
])
def test_each_rule_denies(kw, name):
    assert name in failed(base(**kw))


def test_no_trade_proposal_denied():
    assert "ACTION_IS_TRADE" in failed(base(proposal=TradeProposal(decision_id="d", cycle_id="c", symbol="XAUUSD",
                                                                    action=Action.NO_TRADE, created_utc=T0)))


def test_total_open_risk_limit():
    lim = LoadedRiskLimits(limits=LIMITS.limits.model_copy(update={"max_concurrent_positions": 3}),
                           source_path="x", sha256="x")
    assert "TOTAL_OPEN_RISK" in failed(base(open_positions=(OpenPositionRisk("EURUSD", "BUY", 60.0, "y"),)), lim)


def test_minimum_volume_exceeds_budget():
    assert "POSITION_SIZE_VALID" in failed(base(account=account(equity=100.0), day_start_equity=100.0))


def test_news_unknown_policy():
    assert "NEWS" not in failed(base(news_blackout=None))                # PAPER + ALLOW_PAPER_ONLY
    strict = LoadedRiskLimits(limits=LIMITS.limits.model_copy(update={"news_unknown_policy": "DENY"}), source_path="x", sha256="x")
    assert "NEWS" in failed(base(news_blackout=None), strict)
    assert "NEWS" in failed(base(news_blackout=None, mode=Mode.BROKER_DEMO))


def test_broker_demo_refuses_mock_spec_and_quote():
    f = failed(base(mode=Mode.BROKER_DEMO))
    assert {"SPEC_FROM_BROKER", "QUOTE_FROM_BROKER"} <= f


def test_short_geometry_and_sizing():
    p = prop(action=Action.SHORT, entry=2650.0, sl=2655.0, tp=2639.0)
    g = evaluate(base(proposal=p), LIMITS)
    assert g.result.value == "ALLOW", g.failed


def test_gate_evaluates_all_checks_no_short_circuit():
    g = evaluate(base(kill_switch_on=True, paused=True, data_valid=False), LIMITS)
    assert {"KILL_SWITCH_OFF", "NOT_PAUSED", "DATA_VALID"} <= {c.name for c in g.failed}
    assert g.volume_lots is None

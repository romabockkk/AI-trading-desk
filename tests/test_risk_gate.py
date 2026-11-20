import pytest

from helpers import LIMITS, T0, FIXTURE_SRC, acct, gold_instrument
from tradedesk.config import LoadedRiskLimits, Mode
from tradedesk.risk import sizing
from tradedesk.risk.gate import GateInput, OpenPositionRisk, evaluate
from tradedesk.schemas.market import DataSource, Quote
from tradedesk.schemas.trading import Action, TradeProposal

INST = gold_instrument()


def q(bid=4000.00, ask=4000.04, t=T0, mock=False):
    return Quote(symbol="XAUUSD", time_utc=t, bid=bid, ask=ask,
                 source=DataSource(name="MOCK", is_mock=True) if mock else FIXTURE_SRC)


def prop(action=Action.LONG, entry=4000.04, sl=3999.74, tp=4000.70, did="d1"):
    return TradeProposal(decision_id=did, cycle_id="c", symbol="XAUUSD", action=action, entry=entry, stop_loss=sl,
                         take_profit=tp, created_utc=T0)


def relaxed(**upd):
    return LoadedRiskLimits(limits=LIMITS.limits.model_copy(update=upd), source_path="x", sha256="x")


# A tight 30-cent gold stop with a 4-cent spread: min lot 0.01 = 1 oz ->
# $0.30 + $0.04 spread + $0.06 commission = $0.40 <= $0.50 budget; spread/stop = 13% <= 15%
OK_LIMITS = relaxed(max_effective_leverage=50.0, max_margin_usage_pct=10.0)


def base(**kw):
    d = dict(mode=Mode.PAPER, proposal=prop(), instrument=INST, spec_missing=(), quote=q(), account=acct(), now_utc=T0,
             consecutive_losses=0, open_positions=(), kill_switch_on=False, paused=False, market_open=True,
             data_valid=True, critical_failures=(), risk_manager_approved=True, skeptic_demands_no_trade=False,
             news_blackout=False, atr=1.0)
    d.update(kw)
    return GateInput(**d)


def failed(gi, limits=OK_LIMITS):
    return {c.name for c in evaluate(gi, limits).failed}


def test_allow_with_100_usd_tight_stop_and_relaxed_leverage():
    g = evaluate(base(), OK_LIMITS)
    assert g.result.value == "ALLOW", [c for c in g.failed]
    assert g.volume_lots == 0.01 and g.risk_money == pytest.approx(0.40) and g.risk_money <= 0.5


def test_realistic_gold_spread_blocks_100_usd_trades():
    """With a 10-cent spread the largest affordable stop (~$0.34) makes spread/stop ~30% > 15% -> NO TRADE."""
    f = failed(base(quote=q(4000.00, 4000.10), proposal=prop(entry=4000.10, sl=3999.80, tp=4000.80)))
    assert "SPREAD_TO_STOP_RATIO" in f


def test_shipped_limits_deny_100_usd_gold_on_leverage():
    """With the shipped 5x effective-leverage cap, 1 oz of gold (~$4000) on $100 equity (40x) is refused."""
    f = failed(base(), LIMITS)
    assert "EFFECTIVE_LEVERAGE" in f


def test_100_usd_gold_typical_stop_is_no_trade():
    """$3 stop: min lot risks $3 + spread + commission >> $0.50 budget -> never rounded up -> NO TRADE."""
    gi = base(proposal=prop(sl=3997.04, tp=4006.10))
    g = evaluate(gi, OK_LIMITS)
    assert g.result.value == "DENY" and "MIN_VOLUME_WITHIN_RISK" in {c.name for c in g.failed}
    msg = next(c.detail for c in g.failed if c.name == "MIN_VOLUME_WITHIN_RISK")
    assert "budget $0.5000" in msg
    assert sizing.size_position(INST.spec, 100, 0.5, 4000.04, 3997.04, 0.04) == (0.0, 0.0)


def test_sizing_includes_commission_and_rounds_down():
    lots, rm = sizing.size_position(INST.spec, 10_000, 0.5, 4000.0, 3995.0, 0.10)
    # per lot: (5 + 0.10) x 100 + 6 = 516 -> 50/516 = 0.0968 -> 0.09
    assert lots == 0.09 and rm == pytest.approx(0.09 * 516)
    assert sizing.max_stop_distance_for_budget(INST.spec, 0.5, 0.10) == pytest.approx(0.34)


@pytest.mark.parametrize("kw,name", [
    ({"kill_switch_on": True}, "KILL_SWITCH_OFF"),
    ({"paused": True}, "NOT_PAUSED"),
    ({"market_open": False}, "MARKET_OPEN"),
    ({"critical_failures": ("MACRO",)}, "CRITICAL_AGENTS_OK"),
    ({"risk_manager_approved": False}, "RISK_MANAGER_APPROVED"),
    ({"skeptic_demands_no_trade": True}, "SKEPTIC_NO_DEMAND"),
    ({"data_valid": False}, "DATA_VALID"),
    ({"instrument": None, "spec_missing": ("volume_min",)}, "SPEC_COMPLETE"),
    ({"quote": None}, "QUOTE_FRESH"),
    ({"quote": q(t=T0.replace(hour=9))}, "QUOTE_FRESH"),
    ({"quote": q(mock=True)}, "QUOTE_REAL"),
    ({"quote": q(bid=4000.0, ask=4001.0)}, "SPREAD_LIMIT"),
    ({"account": None}, "ACCOUNT_KNOWN"),
    ({"proposal": prop(tp=4000.30)}, "REWARD_RISK"),
    ({"proposal": prop(sl=3999.94, tp=4001.0)}, "SPREAD_TO_STOP_RATIO"),
    ({"proposal": prop(entry=4003.0, sl=3999.8, tp=4004.0)}, "ENTRY_NEAR_QUOTE"),
    ({"open_positions": (OpenPositionRisk("XAGUSD", "BUY", 0.4, "x"),)}, "MAX_CONCURRENT_POSITIONS"),
    ({"open_positions": (OpenPositionRisk("XAUUSD", "BUY", 0.1, "x"),)}, "NO_AVERAGING_OR_STACKING"),
    ({"already_ordered_decision_ids": frozenset({"d1"})}, "NO_DUPLICATE_ORDER"),
    ({"account": acct(equity=97.9, daily_pnl=-2.1)}, "DAILY_LOSS_LIMIT"),
    ({"consecutive_losses": 2}, "LOSS_STREAK"),
    ({"news_blackout": True}, "NEWS"),
    ({"mode": Mode.RESEARCH}, "MODE_IS_PAPER"),
    ({"account": acct(margin=95.0)}, "FREE_MARGIN"),
])
def test_each_rule_denies(kw, name):
    assert name in failed(base(**kw))


def test_margin_usage_limit():
    assert "MARGIN_USAGE" in failed(base(), relaxed(max_effective_leverage=50.0, max_margin_usage_pct=5.0))


def test_no_trade_proposal_denied():
    assert "ACTION_IS_TRADE" in failed(base(proposal=TradeProposal(decision_id="d", cycle_id="c", symbol="XAUUSD",
                                                                    action=Action.NO_TRADE, created_utc=T0)))


def test_news_unknown_policy():
    assert "NEWS" not in failed(base(news_blackout=None))
    strict = relaxed(max_effective_leverage=50.0, news_unknown_policy="DENY")
    assert "NEWS" in failed(base(news_blackout=None), strict)


def test_short_allowed_symmetric():
    p = prop(action=Action.SHORT, entry=4000.0, sl=4000.30, tp=3999.35)
    assert evaluate(base(proposal=p), OK_LIMITS).result.value == "ALLOW"


def test_gate_evaluates_all_checks():
    g = evaluate(base(kill_switch_on=True, paused=True, data_valid=False), OK_LIMITS)
    assert {"KILL_SWITCH_OFF", "NOT_PAUSED", "DATA_VALID"} <= {c.name for c in g.failed}
    assert g.volume_lots is None

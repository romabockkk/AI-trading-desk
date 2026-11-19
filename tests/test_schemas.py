from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tradedesk.schemas.agents import (AgentName, AgentReport, CRITICAL_AGENTS, PAYLOAD_MODELS, ReportStatus,
                                      critical_failure)
from tradedesk.schemas.market import Candle, DataSource, Quote, Timeframe
from tradedesk.schemas.trading import Action, TradeProposal

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
MOCK = DataSource(name="MOCK", is_mock=True)


def test_candle_rejects_naive_time():
    with pytest.raises(ValidationError, match="timezone-aware"):
        Candle(symbol="XAUUSD", timeframe=Timeframe.M5, open_time_utc=datetime(2026, 1, 1),
               open=1, high=2, low=0.5, close=1.5, tick_volume=1, is_closed=True)


def test_candle_normalizes_to_utc():
    t = datetime(2026, 1, 1, 3, 0, tzinfo=timezone(timedelta(hours=3)))
    c = Candle(symbol="X", timeframe=Timeframe.H1, open_time_utc=t, open=1, high=2, low=0.5, close=1.5,
               tick_volume=0, is_closed=True)
    assert c.open_time_utc.utcoffset() == timedelta(0) and c.open_time_utc.hour == 0


@pytest.mark.parametrize("o,h,l,c", [(10, 9, 8, 9), (10, 12, 11, 11.5), (10, 12, 9, 13)])
def test_candle_inconsistent_ohlc(o, h, l, c):
    with pytest.raises(ValidationError):
        Candle(symbol="X", timeframe=Timeframe.M5, open_time_utc=NOW, open=o, high=h, low=l, close=c,
               tick_volume=0, is_closed=True)


def test_quote_ask_below_bid_rejected():
    with pytest.raises(ValidationError):
        Quote(symbol="XAUUSD", time_utc=NOW, bid=2000.5, ask=2000.0, source=MOCK)


def test_quote_spread_points():
    q = Quote(symbol="XAUUSD", time_utc=NOW, bid=2000.00, ask=2000.25, source=MOCK)
    assert q.spread_points(0.01) == 25


def _p(**kw):
    d = dict(decision_id="d", cycle_id="c", symbol="XAUUSD", created_utc=NOW)
    d.update(kw)
    return TradeProposal(**d)


def test_long_geometry():
    p = _p(action=Action.LONG, entry=2000, stop_loss=1990, take_profit=2020)
    assert p.reward_risk == pytest.approx(2.0)
    with pytest.raises(ValidationError):
        _p(action=Action.LONG, entry=2000, stop_loss=2010)
    with pytest.raises(ValidationError):
        _p(action=Action.LONG, entry=2000, stop_loss=1990, take_profit=1995)


def test_short_geometry():
    _p(action=Action.SHORT, entry=2000, stop_loss=2010, take_profit=1980)
    with pytest.raises(ValidationError):
        _p(action=Action.SHORT, entry=2000, stop_loss=1990)


def test_trade_without_stop_rejected():
    with pytest.raises(ValidationError, match="stop_loss"):
        _p(action=Action.LONG, entry=2000)


def test_no_trade_needs_no_prices():
    assert _p(action=Action.NO_TRADE).reward_risk is None


def test_every_agent_has_payload_schema():
    assert set(PAYLOAD_MODELS) == set(AgentName)
    assert len(AgentName) == 8


def test_macro_payload_allows_unknown_and_rejects_extra():
    M = PAYLOAD_MODELS[AgentName.MACRO]
    m = M(usd_bias="UNKNOWN", gold_bias="UNKNOWN", upcoming_events=[], blackout_active=None,
          data_status="UNKNOWN", summary="no calendar source connected")
    assert m.blackout_active is None
    with pytest.raises(ValidationError):
        M(usd_bias="UNKNOWN", gold_bias="UNKNOWN", upcoming_events=[], blackout_active=None,
          data_status="UNKNOWN", summary="", invented_field=1)


def test_parsed_payload_validates_strictly():
    r = AgentReport(report_id="r", cycle_id="c", agent=AgentName.SKEPTIC, created_utc=NOW,
                    status=ReportStatus.OK, payload={"objections": [], "demand_no_trade": "maybe", "summary": ""})
    with pytest.raises(ValidationError):
        r.parsed_payload()


def test_critical_failure_detection():
    ok = [AgentReport(report_id=a.value, cycle_id="c", agent=a, created_utc=NOW, status=ReportStatus.OK)
          for a in CRITICAL_AGENTS]
    assert critical_failure(ok) == []
    broken = [r if r.agent is not AgentName.MACRO else
              r.model_copy(update={"status": ReportStatus.TIMEOUT}) for r in ok]
    assert critical_failure(broken) == [AgentName.MACRO]
    assert AgentName.RISK_MANAGER in critical_failure([])

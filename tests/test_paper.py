from datetime import timedelta

import pytest

from helpers import T0, candle, quote
from tradedesk.data.instruments import mock_spec
from tradedesk.execution.paper import PaperBroker
from tradedesk.schemas.market import AccountTradeMode, Timeframe

SPEC = mock_spec("XAUUSD")


def decision(conn, did="d1"):
    conn.execute("INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
                 " risk_gate_result, strategy_id, strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (did, "c", T0.isoformat(), "PAPER", "XAUUSD", "LONG", 2650, 2645, "ALLOW", "S", "1"))


def m5(i, o, h, l, c):
    return candle(i, o, h, l, c, Timeframe.M5, t0=T0)


def test_paper_account_is_not_a_broker_demo(conn):
    pb = PaperBroker(conn)
    a = pb.account_info({}, {}, T0)
    assert a.trade_mode is AccountTradeMode.PAPER and a.source.is_mock and a.balance == 10_000


def test_order_idempotent_and_duplicate_logged(conn):
    decision(conn)
    pb = PaperBroker(conn)
    r1 = pb.place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=2645, take_profit=2660,
                        quote=quote(), spec=SPEC, now=T0)
    r2 = pb.place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=2645, take_profit=2660,
                        quote=quote(), spec=SPEC, now=T0)
    assert not r1["duplicate"] and r2["duplicate"]
    assert len(pb.open_positions()) == 1
    assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE event_type='DUPLICATE_ORDER_BLOCKED'").fetchone()[0] == 1
    assert pb.reconcile()["ok"]


def test_requires_stop_loss(conn):
    decision(conn)
    with pytest.raises(ValueError):
        PaperBroker(conn).place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=None,
                                      take_profit=None, quote=quote(), spec=SPEC, now=T0)


def test_stop_loss_closes_with_about_minus_one_r(conn):
    decision(conn)
    pb = PaperBroker(conn, slippage_points=0)
    pb.place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=2645, take_profit=2660,
                   quote=quote(bid=2650.0, ask=2650.0), spec=SPEC, now=T0)
    closed = pb.process_bars("XAUUSD", [m5(0, 2650, 2651, 2649, 2650), m5(1, 2650, 2650.5, 2644, 2646)], SPEC)
    assert closed and closed[0]["reason"] == "STOP_LOSS"
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["r_multiple"] == pytest.approx(-1.0) and t["net_pnl"] == pytest.approx(-50.0)
    assert pb.balance() == pytest.approx(9950.0)


def test_take_profit_and_short(conn):
    decision(conn)
    pb = PaperBroker(conn, slippage_points=0)
    pb.place_order(decision_id="d1", symbol="XAUUSD", side="SELL", volume_lots=0.1, stop_loss=2655, take_profit=2640,
                   quote=quote(bid=2650.0, ask=2650.2), spec=SPEC, now=T0)
    pb.process_bars("XAUUSD", [m5(0, 2650, 2651, 2649, 2650), m5(1, 2648, 2648, 2639, 2640)], SPEC)
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["exit_reason"] == "TAKE_PROFIT" and t["net_pnl"] > 0


def test_close_all_for_emergency(conn):
    decision(conn)
    pb = PaperBroker(conn)
    pb.place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=2645, take_profit=2660,
                   quote=quote(), spec=SPEC, now=T0)
    out = pb.close_all({"XAUUSD": quote()}, {"XAUUSD": SPEC}, T0 + timedelta(minutes=1), "EMERGENCY_STOP")
    assert out and not pb.open_positions()
    assert conn.execute("SELECT exit_reason FROM trades").fetchone()[0] == "EMERGENCY_STOP"


def test_unrealized_and_equity(conn):
    decision(conn)
    pb = PaperBroker(conn, slippage_points=0)
    pb.place_order(decision_id="d1", symbol="XAUUSD", side="BUY", volume_lots=0.1, stop_loss=2645, take_profit=2660,
                   quote=quote(bid=2650.0, ask=2650.0), spec=SPEC, now=T0)
    a = pb.account_info({"XAUUSD": quote(bid=2652.0, ask=2652.2)}, {"XAUUSD": SPEC}, T0)
    assert a.equity == pytest.approx(10_020.0) and a.margin > 0

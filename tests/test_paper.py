"""PaperBroker accounting. Instruments below are TEST FIXTURES with round numbers, not Bybit's real specs."""
from datetime import datetime, timedelta, timezone

import pytest

from helpers import T0, candle
from tradedesk.db import database as db
from tradedesk.execution.paper import Instrument, PaperBroker, PaperError, server_midnights, server_offset_hours
from tradedesk.schemas.market import DataSource, Quote, SymbolSpec, Timeframe

UTC = timezone.utc
SRC = DataSource(name="TEST", is_mock=False)


def gold(commission=6.0, swap_long=-50.0, swap_short=10.0, margin_pct=0.2):
    spec = SymbolSpec(symbol="XAUUSD", asset_class="METAL", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                      contract_size=100, volume_min=0.01, volume_max=50, volume_step=0.01, margin_currency="USD",
                      profit_currency="USD", swap_long=swap_long, swap_short=swap_short, commission_per_lot=commission,
                      trade_allowed=True, source=SRC, fetched_utc=T0)
    return Instrument(spec, margin_pct, "POINTS", "WEDNESDAY", 1.0)


def q(bid, ask, t=T0):
    return Quote(symbol="XAUUSD", time_utc=t, bid=bid, ask=ask, source=SRC)


def decision(conn, did="d1"):
    conn.execute("INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
                 " risk_gate_result, strategy_id, strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (did, "c", T0.isoformat(), "PAPER", "XAUUSD", "LONG", 4000, 3999, "ALLOW", "S", "1"))


def broker(conn, **kw):
    return PaperBroker(conn, initial_balance=kw.pop("initial_balance", 100.0), slippage_points=kw.pop("slip", 0), **kw)


def buy(pb, did="d1", lots=0.01, sl=3999.5, tp=4001.0, quote=None, inst=None, now=T0):
    return pb.place_order(decision_id=did, symbol="XAUUSD", side="BUY", volume_lots=lots, stop_loss=sl, take_profit=tp,
                          quote=quote or q(4000.00, 4000.20), inst=inst or gold(), now=now)


def test_new_account_has_exactly_100_usd(conn):
    pb = broker(conn)
    s = pb.snapshot({}, {}, T0)
    assert pb.balance() == 100.0 and s["equity"] == 100.0 and s["free_margin"] == 100.0 and s["total_pnl"] == 0
    assert conn.execute("SELECT kind, amount FROM account_ledger").fetchall()[0][:] == ("DEPOSIT", 100.0)
    assert pb.account_info({}, {}, T0).trade_mode.value == "PAPER"


def test_open_charges_commission_and_margin(conn):
    decision(conn)
    pb = broker(conn)
    r = buy(pb)
    assert r["price"] == 4000.20                        # long fills at ask
    assert r["commission"] == pytest.approx(0.06)       # 0.01 lot x $6
    assert r["margin"] == pytest.approx(8.0004)         # exposure 4000.20 x 0.01 lot x 100 oz = 4000.20; x 0.2%
    assert pb.balance() == pytest.approx(99.94)
    s = pb.snapshot({"XAUUSD": q(4000.00, 4000.20)}, {"XAUUSD": gold()}, T0)
    assert s["unrealized_pnl"] == pytest.approx(-0.20)  # marked at bid: (4000.00 - 4000.20) x 1 oz
    assert s["equity"] == pytest.approx(99.74) and s["margin"] == pytest.approx(8.0004)
    assert s["free_margin"] == pytest.approx(99.74 - 8.0004)
    assert s["margin_level_pct"] == pytest.approx(99.74 / 8.0004 * 100, abs=0.01)


def test_stop_loss_on_quote_realized_pnl_exact(conn):
    decision(conn)
    pb = broker(conn)
    buy(pb)
    closed = pb.on_quote("XAUUSD", q(3999.40, 3999.60, T0 + timedelta(seconds=5)), gold(), T0 + timedelta(seconds=5))
    assert closed and closed[0]["reason"] == "STOP_LOSS"
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["exit_price"] == 3999.40                    # gap through stop: filled at the worse bid
    assert t["gross_pnl"] == pytest.approx(-0.80)        # (3999.40 - 4000.20) x 1 oz
    assert t["net_pnl"] == pytest.approx(-0.86)          # minus $0.06 commission
    assert pb.balance() == pytest.approx(100 - 0.86)
    assert pb.reconcile()["ok"]


def test_take_profit_short(conn):
    decision(conn)
    pb = broker(conn)
    pb.place_order(decision_id="d1", symbol="XAUUSD", side="SELL", volume_lots=0.02, stop_loss=4001.0, take_profit=3998.0,
                   quote=q(4000.00, 4000.20), inst=gold(), now=T0)
    pb.on_quote("XAUUSD", q(3997.80, 3997.95, T0 + timedelta(seconds=1)), gold(), T0 + timedelta(seconds=1))
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["exit_reason"] == "TAKE_PROFIT" and t["gross_pnl"] == pytest.approx((4000.00 - 3998.0) * 2)
    assert t["net_pnl"] == pytest.approx(4.0 - 0.12)


def test_slippage_against_trader(conn):
    decision(conn)
    pb = broker(conn, slip=5)
    r = buy(pb, sl=3999.0)
    assert r["price"] == pytest.approx(4000.25)


def test_order_validation(conn):
    decision(conn)
    pb = broker(conn)
    with pytest.raises(PaperError, match="stale"):
        buy(pb, quote=q(4000, 4000.2, T0 - timedelta(seconds=30)))
    with pytest.raises(PaperError, match="multiple of step"):
        buy(pb, lots=0.015)
    with pytest.raises(PaperError, match="outside"):
        buy(pb, lots=0.001)
    with pytest.raises(PaperError, match="wrong side"):
        buy(pb, sl=4001.0)
    with pytest.raises(PaperError, match="mandatory"):
        buy(pb, sl=None)
    with pytest.raises(PaperError, match="insufficient free margin"):
        buy(pb, lots=0.2)         # margin 160 > 100


def test_idempotent_and_no_stacking(conn):
    decision(conn)
    decision(conn, "d2")
    pb = broker(conn)
    buy(pb)
    assert buy(pb)["duplicate"] is True
    with pytest.raises(PaperError, match="already open"):
        buy(pb, did="d2")
    assert len(pb.open_positions()) == 1
    assert conn.execute("SELECT COUNT(*) FROM risk_events WHERE event_type='DUPLICATE_ORDER_BLOCKED'").fetchone()[0] == 1


def test_state_survives_restart(tmp_path):
    p = tmp_path / "x.db"
    c1 = db.init_db(p)
    decision(c1)
    pb = broker(c1)
    buy(pb)
    aid = pb.account_id
    c1.close()
    c2 = db.init_db(p)
    pb2 = broker(c2)
    assert pb2.account_id == aid and len(pb2.open_positions()) == 1 and pb2.balance() == pytest.approx(99.94)


def test_ledger_append_only(conn):
    broker(conn)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE account_ledger SET amount=1000000")


def test_server_time_and_swap(conn):
    assert server_offset_hours(datetime(2026, 1, 15, tzinfo=UTC)) == 2
    assert server_offset_hours(datetime(2026, 7, 15, tzinfo=UTC)) == 3
    assert server_offset_hours(datetime(2026, 11, 1, 1, 59, tzinfo=UTC)) == 3     # 1st Sunday of Nov 2026 = Nov 1
    assert server_offset_hours(datetime(2026, 11, 1, 2, 0, tzinfo=UTC)) == 2
    m = server_midnights(datetime(2026, 6, 9, 12, tzinfo=UTC), datetime(2026, 6, 11, 12, tzinfo=UTC))
    assert m == [datetime(2026, 6, 9, 21, tzinfo=UTC), datetime(2026, 6, 10, 21, tzinfo=UTC)]   # 00:00 GMT+3
    decision(conn)
    pb = broker(conn)
    t_open = datetime(2026, 6, 9, 12, tzinfo=UTC)                    # Tuesday
    buy(pb, quote=q(4000, 4000.2, t_open), now=t_open)
    pb.apply_swaps({"XAUUSD": gold()}, datetime(2026, 6, 11, 12, tzinfo=UTC))
    p = pb.open_positions()[0]
    night = -50 * 0.01 * 100 * 0.01                                   # -50 points x $0.01 x 100 oz x 0.01 lot = -0.50
    assert p["swap_accrued"] == pytest.approx(night * 1 + night * 3)   # Tue night x1, Wed night x3
    pb.apply_swaps({"XAUUSD": gold()}, datetime(2026, 6, 11, 12, tzinfo=UTC))     # idempotent
    assert pb.open_positions()[0]["swap_accrued"] == pytest.approx(night * 4)
    s = pb.snapshot({"XAUUSD": q(4000.2, 4000.4, datetime(2026, 6, 11, 12, tzinfo=UTC))}, {"XAUUSD": gold()},
                    datetime(2026, 6, 11, 12, tzinfo=UTC))
    assert s["swap_accrued"] == pytest.approx(night * 4)
    pb.close_all({"XAUUSD": q(4000.2, 4000.4)}, {"XAUUSD": gold()}, datetime(2026, 6, 11, 12, tzinfo=UTC), "MANUAL")
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["swap"] == pytest.approx(night * 4) and pb.reconcile()["ok"]


def test_bars_catch_stop_between_polls(conn):
    decision(conn)
    pb = broker(conn)
    buy(pb)
    bars = [candle(0, 4000.1, 4000.3, 4000.0, 4000.2, Timeframe.M5, t0=T0, spread=20),
            candle(1, 4000.2, 4000.4, 3999.3, 3999.6, Timeframe.M5, t0=T0, spread=20)]
    out = pb.on_bars("XAUUSD", bars, gold())
    assert out and out[0]["reason"] == "STOP_LOSS"
    assert conn.execute("SELECT exit_price FROM trades").fetchone()[0] == pytest.approx(3999.5)


def test_stop_out_closes_positions(conn):
    decision(conn)
    pb = broker(conn, stop_out_level_pct=50)
    buy(pb, lots=0.1, sl=3900.0, tp=None)       # 10 oz, margin 80.0, commission 0.60
    bad = q(3996.0, 3996.2, T0 + timedelta(seconds=1))   # upnl -42 -> equity 57.4 -> 71.7% > 50
    assert pb.check_stop_out({"XAUUSD": bad}, {"XAUUSD": gold()}, T0) == []
    worse = q(3992.0, 3992.2, T0 + timedelta(seconds=2))  # upnl -82 -> equity 17.4 -> 21.7% <= 50 -> stop-out
    assert pb.check_stop_out({"XAUUSD": worse}, {"XAUUSD": gold()}, T0) != []
    assert not pb.open_positions()


def test_reset_requires_owner_and_archives(conn):
    decision(conn)
    pb = broker(conn)
    buy(pb)
    with pytest.raises(PaperError):
        pb.reset("CHIEF", "agents want money")
    with pytest.raises(PaperError, match="close them first"):
        pb.reset("OWNER", "fresh start")
    old = pb.account_id
    r = pb.reset("OWNER", "fresh start", close_quotes={"XAUUSD": q(4000, 4000.2)}, instruments={"XAUUSD": gold()}, now=T0)
    assert r["archived"] == old and pb.balance() == 100.0 and pb.account_id != old
    assert conn.execute("SELECT status FROM paper_accounts WHERE account_id=?", (old,)).fetchone()[0] == "ARCHIVED"
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE account_id=?", (old,)).fetchone()[0] == 1   # history kept


def test_daily_pnl_tracks_from_day_start(conn):
    decision(conn)
    pb = broker(conn)
    pb.snapshot({}, {}, T0)
    buy(pb)
    s = pb.snapshot({"XAUUSD": q(4000.0, 4000.2)}, {"XAUUSD": gold()}, T0)
    assert s["daily_pnl"] == pytest.approx(-0.26) and s["total_pnl"] == pytest.approx(-0.26)


def test_snapshot_reports_week_and_trades_today(conn):
    decision(conn)
    pb = broker(conn)
    buy(pb)
    s = pb.snapshot({"XAUUSD": q(4000.0, 4000.2)}, {"XAUUSD": gold()}, T0)
    assert s["trades_today"] == 1 and s["week_start_equity"] == 100.0 and s["weekly_pnl"] == pytest.approx(-0.26)

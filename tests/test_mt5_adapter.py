"""MT5Adapter against a FAKE MetaTrader5 module. Proves guard logic only — not a broker connection."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from helpers import settings
from tradedesk.broker.base import BrokerError
from tradedesk.broker.mt5_adapter import MT5Adapter
from tradedesk.config import OWNER_DEMO_AUTH_PHRASE
from tradedesk.safety import DemoGuardViolation
from tradedesk.schemas.market import AccountTradeMode, Timeframe


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, POSITION_TYPE_BUY = 0, 1, 0
    ORDER_TIME_GTC, ORDER_FILLING_IOC = 0, 1
    TIMEFRAME_M5 = 5

    def __init__(self, trade_mode=0, server="Bybit-Demo"):
        self.acc = NS(login=123, server=server, company="Bybit", trade_mode=trade_mode, currency="USD", balance=10000.0,
                      equity=10000.0, margin=0.0, margin_free=10000.0, leverage=100, trade_allowed=True)
        self.sent, self.pos = [], []

    def initialize(self, **kw):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return self.acc

    def symbol_info_tick(self, s):
        return NS(time=int(datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc).timestamp()), bid=2650.0, ask=2650.25)

    def positions_get(self, symbol=None):
        return self.pos

    def orders_get(self, symbol=None):
        return []

    def order_send(self, req):
        self.sent.append(req)
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.pos.append(NS(ticket=1, symbol=req["symbol"], volume=req["volume"], sl=req["sl"], comment=req["comment"],
                               type=req["type"]))
        return NS(retcode=10009, order=1, deal=1, price=req.get("price"), volume=req.get("volume"))

    def copy_rates_range(self, s, tf, a, b):
        t = int(datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc).timestamp())   # server wall time 15:00 (UTC+2)
        return [{"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 10, "spread": 20}]


def adapter(fake, ok=True, **skw):
    s = settings(tradedesk_mode="BROKER_DEMO", tradedesk_broker_demo_enabled=True,
                 tradedesk_owner_demo_authorization=OWNER_DEMO_AUTH_PHRASE, **skw)
    a = MT5Adapter(s, lambda: (ok, False, False), server_tz="Etc/GMT-2", mt5_module=fake)
    a.connect()
    return a


def test_connect_refuses_real_account():
    with pytest.raises(BrokerError, match="not DEMO"):
        adapter(FakeMT5(trade_mode=2))


def test_server_time_converted_to_utc():
    a = adapter(FakeMT5())
    assert a.quote("XAUUSD+").time_utc == datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc)
    c = a.candles("XAUUSD+", Timeframe.M5, datetime(2026, 6, 10, tzinfo=timezone.utc), datetime(2026, 6, 11, tzinfo=timezone.utc))
    assert c[0].open_time_utc.hour == 13 and not c[0].is_closed is None
    assert a.account_info().trade_mode is AccountTradeMode.DEMO and not a.account_info().source.is_mock


def test_order_sent_only_when_all_guards_pass_and_duplicates_blocked():
    fake = FakeMT5()
    a = adapter(fake)
    r = a.place_demo_order(client_order_id="DEMO-d1", symbol="XAUUSD+", side="BUY", volume_lots=0.1, stop_loss=2645,
                           take_profit=2660)
    assert r["confirmed_position"] and fake.sent[0]["sl"] == 2645
    with pytest.raises(BrokerError, match="duplicate"):
        a.place_demo_order(client_order_id="DEMO-d1", symbol="XAUUSD+", side="BUY", volume_lots=0.1, stop_loss=2645,
                           take_profit=2660)
    assert a.reconcile([{"client_order_id": "DEMO-d1", "volume_lots": 0.1}])["ok"]


def test_guard_blocks_when_account_switches_to_real_mid_session():
    fake = FakeMT5()
    a = adapter(fake)
    fake.acc.trade_mode = 2
    with pytest.raises(DemoGuardViolation):
        a.place_demo_order(client_order_id="x", symbol="XAUUSD+", side="BUY", volume_lots=0.1, stop_loss=1, take_profit=None)
    assert fake.sent == []


def test_guard_blocks_without_integration_tests_or_with_wrong_server():
    with pytest.raises(DemoGuardViolation):
        adapter(FakeMT5(), ok=False).place_demo_order(client_order_id="x", symbol="X", side="BUY", volume_lots=0.1,
                                                     stop_loss=1, take_profit=None)
    with pytest.raises(DemoGuardViolation):
        adapter(FakeMT5(server="Bybit-Live")).place_demo_order(client_order_id="x", symbol="X", side="BUY",
                                                              volume_lots=0.1, stop_loss=1, take_profit=None)


def test_stop_loss_mandatory_and_cannot_be_removed():
    a = adapter(FakeMT5())
    with pytest.raises(BrokerError):
        a.place_demo_order(client_order_id="x", symbol="X", side="BUY", volume_lots=0.1, stop_loss=None, take_profit=None)
    with pytest.raises(BrokerError):
        a.modify_demo_order(broker_ticket="1", stop_loss=None, take_profit=None)


def test_real_package_unavailable_on_linux():
    import platform
    if platform.system() == "Windows":
        pytest.skip("windows host")
    a = MT5Adapter(settings(), lambda: (False, False, False))
    with pytest.raises(Exception, match="MetaTrader5"):
        a.connect()

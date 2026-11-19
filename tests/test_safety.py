from datetime import datetime, timezone

import pytest

from tradedesk.broker.base import BrokerAdapter
from tradedesk.broker.status import broker_demo_status
from tradedesk.config import OWNER_DEMO_AUTH_PHRASE, Settings
from tradedesk.safety import (DemoGuardViolation, can_send_broker_order, require_broker_order_allowed,
                              verify_demo_account)
from tradedesk.schemas.market import AccountInfo, AccountTradeMode, DataSource

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def acct(trade_mode=AccountTradeMode.DEMO, server="Bybit-Demo", mock=False):
    return AccountInfo(login="1", server=server, trade_mode=trade_mode, currency="USD", balance=10000,
                       equity=10000, margin=0, margin_free=10000, leverage=100, trade_allowed=True,
                       source=DataSource(name="MT5", is_mock=mock), fetched_utc=NOW)


def full_settings(**kw):
    d = dict(_env_file=None, tradedesk_mode="BROKER_DEMO", tradedesk_broker_demo_enabled=True,
             tradedesk_owner_demo_authorization=OWNER_DEMO_AUTH_PHRASE, tradedesk_allowed_demo_servers="Bybit-Demo")
    d.update(kw)
    return Settings(**d)


def test_demo_account_passes():
    assert verify_demo_account(acct(), ("Bybit-Demo",)).allowed


@pytest.mark.parametrize("a,frag", [
    (acct(trade_mode=AccountTradeMode.REAL), "not DEMO"),
    (acct(trade_mode=AccountTradeMode.UNKNOWN), "not DEMO"),
    (acct(trade_mode=AccountTradeMode.CONTEST), "not DEMO"),
    (acct(server="Bybit-Live"), "allowlist"),
    (acct(server="Bybit-Demo2"), "allowlist"),
    (acct(mock=True), "mock"),
    (None, "unavailable"),
])
def test_demo_account_failures(a, frag):
    r = verify_demo_account(a, ("Bybit-Demo",))
    assert not r.allowed and any(frag in x for x in r.reasons)


def test_real_server_in_allowlist_still_blocked_by_name():
    r = verify_demo_account(acct(server="Bybit-Real"), ("Bybit-Real",))
    assert not r.allowed


def test_all_conditions_met_allows():
    r = can_send_broker_order(full_settings(), acct(), integration_tests_passed=True,
                              kill_switch_on=False, paused=False)
    assert r.allowed, r.reasons


@pytest.mark.parametrize("skw,kw", [
    ({"tradedesk_mode": "PAPER"}, {}),
    ({"tradedesk_broker_demo_enabled": False}, {}),
    ({"tradedesk_owner_demo_authorization": ""}, {}),
    ({}, {"integration_tests_passed": False}),
    ({}, {"kill_switch_on": True}),
    ({}, {"paused": True}),
])
def test_any_missing_condition_blocks(skw, kw):
    args = dict(integration_tests_passed=True, kill_switch_on=False, paused=False)
    args.update(kw)
    with pytest.raises(DemoGuardViolation):
        require_broker_order_allowed(full_settings(**skw), acct(), **args)


def test_default_settings_block_everything():
    r = can_send_broker_order(Settings(_env_file=None), acct(), integration_tests_passed=False,
                              kill_switch_on=False, paused=False)
    assert not r.allowed and len(r.reasons) >= 4


def test_broker_demo_status_is_blocked_here():
    st = broker_demo_status(Settings(_env_file=None))
    assert st["status"] == "BLOCKED" and st["connected"] is False
    assert any("MetaTrader5" in r for r in st["reasons"])


def test_broker_adapter_interface_complete():
    required = {"connect", "account_info", "is_demo", "symbols", "symbol_spec", "quote", "candles", "positions",
                "place_demo_order", "modify_demo_order", "close_demo_position", "reconcile", "disconnect"}
    assert required <= BrokerAdapter.__abstractmethods__

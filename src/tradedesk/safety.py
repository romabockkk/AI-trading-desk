"""Deterministic safety guards. No LLM output can change these results.

Order-sending to a broker is allowed only if EVERY condition holds:
  1. Mode is BROKER_DEMO.
  2. TRADEDESK_BROKER_DEMO_ENABLED is true.
  3. Owner authorization phrase is configured.
  4. Broker integration tests have passed (recorded flag).
  5. Kill switch is OFF and system is not paused.
  6. The *live* account info proves DEMO: trade_mode == DEMO, server in the
     owner allowlist (exact match), server name contains 'demo', data not mock.
Anything unknown => blocked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Mode, Settings
from .schemas.market import AccountInfo, AccountTradeMode


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


class DemoGuardViolation(RuntimeError):
    """Raised when a broker order is attempted without proven DEMO conditions."""


def verify_demo_account(account: AccountInfo | None, allowed_servers: tuple[str, ...]) -> GuardResult:
    reasons: list[str] = []
    if account is None:
        return GuardResult(False, ("account info unavailable: cannot prove DEMO",))
    if account.source.is_mock:
        reasons.append("account info comes from a mock source; mock is never a broker DEMO")
    if account.trade_mode is not AccountTradeMode.DEMO:
        reasons.append(f"account trade_mode is {account.trade_mode.value}, not DEMO")
    if not allowed_servers:
        reasons.append("no allowed demo servers configured")
    elif account.server not in allowed_servers:
        reasons.append(f"server {account.server!r} not in owner allowlist {list(allowed_servers)}")
    if "demo" not in account.server.lower():
        reasons.append(f"server name {account.server!r} does not identify a demo server")
    return GuardResult(not reasons, tuple(reasons))


def can_send_broker_order(settings: Settings, account: AccountInfo | None, *,
                          integration_tests_passed: bool, kill_switch_on: bool, paused: bool) -> GuardResult:
    reasons: list[str] = []
    if settings.mode is not Mode.BROKER_DEMO:
        reasons.append(f"mode is {settings.mode.value}, not BROKER_DEMO")
    if not settings.tradedesk_broker_demo_enabled:
        reasons.append("BROKER_DEMO disabled (TRADEDESK_BROKER_DEMO_ENABLED=false)")
    if not settings.owner_demo_authorized:
        reasons.append("owner authorization for demo orders not given")
    if not integration_tests_passed:
        reasons.append("broker integration tests have not passed")
    if kill_switch_on:
        reasons.append("kill switch is ON")
    if paused:
        reasons.append("system is paused")
    demo = verify_demo_account(account, settings.allowed_demo_servers)
    reasons.extend(demo.reasons)
    return GuardResult(not reasons, tuple(reasons))


def require_broker_order_allowed(*args, **kwargs) -> None:
    res = can_send_broker_order(*args, **kwargs)
    if not res.allowed:
        raise DemoGuardViolation("; ".join(res.reasons))

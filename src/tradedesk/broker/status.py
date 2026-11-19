"""Honest BROKER_DEMO availability report. Never reports READY without proof."""
from __future__ import annotations

import importlib.util
import platform

from ..config import Settings

# Findings from Stage 1 audit (docs/AUDIT.md). Re-verify before Stage 10.
DOC_FINDINGS = (
    "Bybit help page 'How to get started with Bybit CFD Demo Trading' (as quoted by web search, "
    "page not directly fetchable from this container) states MT5 Client, API and Expert Advisors "
    "access are NOT supported for CFD Demo at launch.",
)


def broker_demo_status(settings: Settings, *, integration_tests_passed: bool = False) -> dict:
    reasons: list[str] = []
    system = platform.system()
    if system != "Windows":
        reasons.append(f"host OS is {system}; MetaTrader5 Python package requires Windows + MT5 terminal")
    if importlib.util.find_spec("MetaTrader5") is None:
        reasons.append("MetaTrader5 Python package not installed (no Linux wheels on PyPI)")
    if not settings.tradedesk_broker_demo_enabled:
        reasons.append("TRADEDESK_BROKER_DEMO_ENABLED=false (default)")
    if not settings.owner_demo_authorized:
        reasons.append("owner has not authorized demo orders")
    if not integration_tests_passed:
        reasons.append("broker integration tests not run/passed")
    reasons.extend(DOC_FINDINGS)
    return {
        "status": "BLOCKED" if reasons else "READY_FOR_VERIFICATION",
        "connected": False,
        "reasons": reasons,
    }

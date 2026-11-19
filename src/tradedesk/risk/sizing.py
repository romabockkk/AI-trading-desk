"""Position sizing from the real contract specification. Pure functions."""
from __future__ import annotations

import math

from ..schemas.market import SymbolSpec


def money_per_price_unit(spec: SymbolSpec, lots: float) -> float:
    """P/L in account currency for a 1.0 price move with `lots`."""
    return lots * spec.tick_value / spec.tick_size


def risk_money(spec: SymbolSpec, lots: float, entry: float, stop: float, spread_price: float = 0.0) -> float:
    return money_per_price_unit(spec, lots) * (abs(entry - stop) + spread_price)


def round_volume_down(spec: SymbolSpec, lots: float) -> float:
    steps = math.floor(lots / spec.volume_step + 1e-9)
    v = round(steps * spec.volume_step, 8)
    return min(v, spec.volume_max)


def size_position(spec: SymbolSpec, equity: float, risk_pct: float, entry: float, stop: float,
                  spread_price: float = 0.0) -> tuple[float, float]:
    """Largest volume (rounded DOWN to step) whose stop-out loss incl. spread <= risk_pct of equity.

    Returns (lots, risk_money). lots == 0 means the minimum volume would exceed the risk budget.
    """
    if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        return 0.0, 0.0
    budget = equity * risk_pct / 100.0
    per_lot = risk_money(spec, 1.0, entry, stop, spread_price)
    if per_lot <= 0:
        return 0.0, 0.0
    lots = round_volume_down(spec, budget / per_lot)
    if lots < spec.volume_min:
        return 0.0, 0.0
    return lots, risk_money(spec, lots, entry, stop, spread_price)


def notional(spec: SymbolSpec, lots: float, price: float, account_ccy: str = "USD") -> float:
    if spec.symbol.startswith(account_ccy):
        return lots * spec.contract_size           # e.g. USDJPY: base currency is account currency
    return lots * spec.contract_size * price


def required_margin(spec: SymbolSpec, lots: float, price: float, leverage: int, account_ccy: str = "USD") -> float:
    return notional(spec, lots, price, account_ccy) / max(leverage, 1)

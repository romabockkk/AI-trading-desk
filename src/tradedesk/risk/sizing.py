"""Position sizing from the instrument specification. Pure functions. Volume is always rounded DOWN."""
from __future__ import annotations

import math

from ..schemas.market import SymbolSpec


def money_per_price_unit(spec: SymbolSpec, lots: float) -> float:
    """P/L in account currency for a 1.0 price move with `lots` (uses the derived tick value)."""
    return lots * spec.tick_value / spec.tick_size


def risk_money(spec: SymbolSpec, lots: float, entry: float, stop: float, spread_price: float = 0.0,
               commission_per_lot: float = 0.0) -> float:
    """Loss if the stop is hit: price distance (+ spread to exit) plus commission."""
    return money_per_price_unit(spec, lots) * (abs(entry - stop) + spread_price) + lots * commission_per_lot


def round_volume_down(spec: SymbolSpec, lots: float) -> float:
    steps = math.floor(lots / spec.volume_step + 1e-9)
    return min(round(steps * spec.volume_step, 8), spec.volume_max)


def size_position(spec: SymbolSpec, equity: float, risk_pct: float, entry: float, stop: float,
                  spread_price: float = 0.0, commission_per_lot: float | None = None) -> tuple[float, float]:
    """Largest volume (rounded DOWN to the step) whose stop-out loss incl. spread and commission <= risk budget.

    Returns (lots, risk_money). lots == 0 means even the minimum volume would exceed the budget => NO TRADE.
    Never rounds up to reach the minimum volume.
    """
    if equity <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        return 0.0, 0.0
    comm = spec.commission_per_lot if commission_per_lot is None else commission_per_lot
    comm = comm or 0.0
    budget = equity * risk_pct / 100.0
    per_lot = risk_money(spec, 1.0, entry, stop, spread_price, comm)
    if per_lot <= 0:
        return 0.0, 0.0
    lots = round_volume_down(spec, budget / per_lot)
    if lots < spec.volume_min - 1e-12:
        return 0.0, 0.0
    return lots, risk_money(spec, lots, entry, stop, spread_price, comm)


def min_lot_risk(spec: SymbolSpec, entry: float, stop: float, spread_price: float = 0.0) -> float:
    return risk_money(spec, spec.volume_min, entry, stop, spread_price, spec.commission_per_lot or 0.0)


def max_stop_distance_for_budget(spec: SymbolSpec, budget: float, spread_price: float = 0.0) -> float:
    """Largest stop distance (price units) the minimum volume can use within `budget`. <= 0 => impossible."""
    vpp = money_per_price_unit(spec, spec.volume_min)
    return (budget - spec.volume_min * (spec.commission_per_lot or 0.0)) / vpp - spread_price

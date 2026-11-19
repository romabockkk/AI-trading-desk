"""Instrument catalogue.

MOCK specifications are *assumptions* used only for PAPER/BACKTEST when no broker
spec is available. They are tagged is_mock=True and the Risk Gate refuses them in
BROKER_DEMO. Real values must come from the broker (MT5 symbol_info).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..schemas.market import DataSource, SymbolSpec

MOCK_SPEC_SOURCE = DataSource(name="MOCK_SPEC (assumed, not from broker)", is_mock=True)
_FETCHED = datetime(2026, 11, 19, tzinfo=timezone.utc)

# name -> (asset_class, digits, point, contract_size, start_price, daily_vol_pct, typical_spread_points)
_MOCK = {
    "XAUUSD": ("METAL", 2, 0.01, 100.0, 2650.0, 1.1, 25),
    "XAGUSD": ("METAL", 3, 0.001, 5000.0, 31.0, 1.8, 30),
    "EURUSD": ("FX", 5, 0.00001, 100000.0, 1.08, 0.5, 12),
    "GBPUSD": ("FX", 5, 0.00001, 100000.0, 1.27, 0.6, 15),
    "USDJPY": ("FX", 3, 0.001, 100000.0, 150.0, 0.6, 14),
    "US500": ("INDEX", 2, 0.01, 1.0, 5900.0, 1.0, 60),
}

# Instruments the desk researches, in priority order (gold first).
PRIORITY = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY", "US500")


def mock_params(symbol: str) -> tuple:
    if symbol not in _MOCK:
        raise KeyError(f"no mock parameters for {symbol}")
    return _MOCK[symbol]


def mock_spec(symbol: str) -> SymbolSpec:
    cls, digits, point, contract, _, _, _ = mock_params(symbol)
    # tick value in USD for 1 lot and one point move. For USDJPY profit currency is JPY:
    # approximated at the mock start price; flagged as mock.
    tick_value = contract * point
    if symbol == "USDJPY":
        tick_value = contract * point / 150.0
    return SymbolSpec(
        symbol=symbol, description=f"{symbol} (MOCK spec)", asset_class=cls, digits=digits,
        point=point, tick_size=point, tick_value=tick_value, contract_size=contract,
        volume_min=0.01, volume_max=50.0, volume_step=0.01, margin_currency="USD",
        profit_currency="JPY" if symbol == "USDJPY" else "USD", stops_level_points=0,
        swap_long=None, swap_short=None, commission_per_lot=0.0, trade_allowed=True,
        source=MOCK_SPEC_SOURCE, fetched_utc=_FETCHED,
    )

"""READ-ONLY MetaTrader 5 market data provider (Windows host with an MT5 terminal only).

Owner directive 2026-11-20: no orders to any broker. This module therefore contains no order functions at all.
It can read quotes, candles and — most usefully — the broker's real symbol specifications
(contract size, volume min/step/max, tick size, swaps) to fill config gaps.

The official MetaTrader5 Python package is Windows-only (PyPI: win_amd64 wheels). Not usable on Linux.
Bybit's CFD Demo does not offer MT5 client/API access at launch (Bybit Learn, 2026-11-05); a live MT5 CFD
account could be read with this provider, but that is the owner's decision.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..schemas.market import Candle, DataSource, Quote, Timeframe
from .providers import ProviderError, ProviderQuote


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as e:
        raise ProviderError("MetaTrader5 package not installed (Windows-only)") from e
    return mt5


def server_epoch_to_utc(epoch: int, offset_hours: int) -> datetime:
    """MT5 returns server wall time encoded as a UTC epoch; subtract the server offset."""
    return datetime.fromtimestamp(int(epoch) - offset_hours * 3600, tz=timezone.utc)


class MT5ReadOnlyProvider:
    name = "MT5"

    def __init__(self, symbol_map: dict[str, str], mt5_module=None, offset_fn=None, **init_kwargs):
        from ..execution.paper import server_offset_hours
        self.map = symbol_map
        self.mt5 = mt5_module
        self.offset_fn = offset_fn or server_offset_hours
        self.init_kwargs = init_kwargs
        self.source = DataSource(name="MT5 terminal (read-only)", is_mock=False)

    def _mt5(self):
        if self.mt5 is None:
            self.mt5 = _load_mt5()
        if not self.mt5.initialize(**self.init_kwargs):
            raise ProviderError(f"MT5 initialize failed: {self.mt5.last_error()}")
        return self.mt5

    def supports(self, symbol: str) -> bool:
        return symbol in self.map

    def quotes(self, symbols: list[str], now: datetime | None = None) -> dict[str, ProviderQuote]:
        mt5 = self._mt5()
        out = {}
        for s in symbols:
            if s not in self.map:
                continue
            t = mt5.symbol_info_tick(self.map[s])
            if t is None:
                continue
            approx = datetime.fromtimestamp(int(t.time), tz=timezone.utc)
            out[s] = ProviderQuote(Quote(symbol=s, time_utc=server_epoch_to_utc(t.time, self.offset_fn(approx)),
                                         bid=t.bid, ask=t.ask, source=self.source), None)
        return out

    def candles(self, symbol: str, tf: Timeframe, count: int = 500, now: datetime | None = None) -> list[Candle]:
        mt5 = self._mt5()
        rates = mt5.copy_rates_from_pos(self.map[symbol], getattr(mt5, f"TIMEFRAME_{tf.value}"), 0, count)
        if rates is None:
            raise ProviderError(f"copy_rates_from_pos failed: {mt5.last_error()}")
        now = now or datetime.now(timezone.utc)
        out = []
        for r in rates:
            approx = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            t = server_epoch_to_utc(r["time"], self.offset_fn(approx))
            out.append(Candle(symbol=symbol, timeframe=tf, open_time_utc=t, open=float(r["open"]), high=float(r["high"]),
                              low=float(r["low"]), close=float(r["close"]), tick_volume=float(r["tick_volume"]),
                              spread_points=float(r["spread"]), is_closed=t.timestamp() + tf.seconds <= now.timestamp()))
        return out

    def read_spec_fields(self, symbol: str) -> dict:
        """Real broker spec fields for specs.update(..., source='MT5 symbol_info ...')."""
        i = self._mt5().symbol_info(self.map[symbol])
        if i is None:
            raise ProviderError(f"symbol_info({symbol}) unavailable")
        days = {0: "SUNDAY", 1: "MONDAY", 2: "TUESDAY", 3: "WEDNESDAY", 4: "THURSDAY", 5: "FRIDAY", 6: "SATURDAY"}
        return {"contract_size": i.trade_contract_size, "volume_min": i.volume_min, "volume_step": i.volume_step,
                "volume_max": i.volume_max, "tick_size": i.trade_tick_size, "digits": i.digits,
                "swap_long": i.swap_long, "swap_short": i.swap_short,
                "swap_triple_day": days.get(getattr(i, "swap_rollover3days", -1)), "profit_currency": i.currency_profit}

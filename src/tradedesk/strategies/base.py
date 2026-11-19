"""Strategy framework.

Contract (prevents look-ahead):
- `prepare()` receives only CLOSED candles of the execution timeframe and of the
  higher timeframes, and may precompute *causal* series.
- `signal_at(ctx, i)` may read bars [0..i] of the execution timeframe and only
  higher-timeframe bars whose close time <= close time of bar i (use ctx.htf_index).
- A signal at bar i is executed at the OPEN of bar i+1 (never at bar i's close).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import timedelta

from ..analysis.indicators import atr, ema
from ..schemas.market import Candle, Timeframe

ROLLOVER_HOURS_UTC = frozenset({20, 21, 22})   # wide spreads around daily rollover


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    version: str
    index: int
    time_utc: str
    action: str            # LONG | SHORT
    ref_price: float       # close of signal bar (entry happens next bar open)
    stop_loss: float
    take_profit: float
    invalidation: str
    reason: str
    rule_ids: tuple[str, ...] = ()
    rr_target: float | None = None   # if set, TP is re-anchored to the actual fill: fill +/- rr * |fill - SL|

    def target_for_entry(self, entry: float) -> float:
        if self.rr_target is None:
            return self.take_profit
        r = abs(entry - self.stop_loss) * self.rr_target
        return entry + r if self.action == "LONG" else entry - r

    @property
    def planned_rr(self) -> float:
        risk = abs(self.ref_price - self.stop_loss)
        return abs(self.take_profit - self.ref_price) / risk if risk > 0 else 0.0


@dataclass
class Context:
    bars: list[Candle]
    htf: dict[Timeframe, list[Candle]]
    exec_tf: Timeframe
    atr14: list[float | None]
    closes: list[float]
    cache: dict = field(default_factory=dict)
    _htf_close_times: dict = field(default_factory=dict)

    def htf_index(self, tf: Timeframe, i: int) -> int:
        """Index of the last HTF bar fully closed at the close of exec bar i (or -1)."""
        if tf not in self._htf_close_times:
            self._htf_close_times[tf] = [b.open_time_utc + timedelta(seconds=tf.seconds) for b in self.htf.get(tf, [])]
        close_i = self.bars[i].open_time_utc + timedelta(seconds=self.exec_tf.seconds)
        return bisect_right(self._htf_close_times[tf], close_i) - 1


class Strategy:
    id: str = "base"
    version: str = "1.0.0"
    exec_tf: Timeframe = Timeframe.M15
    htfs: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4)
    rules: str = ""
    default_params: dict = {}
    param_grid: dict[str, list] = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.id}: unknown params {unknown}")
        self.params = {**self.default_params, **params}

    def make_context(self, bars: list[Candle], htf: dict[Timeframe, list[Candle]]) -> Context:
        if any(not b.is_closed for b in bars):
            raise ValueError("strategies accept closed candles only")
        ctx = Context(bars=bars, htf=htf, exec_tf=self.exec_tf, atr14=atr(bars, 14), closes=[b.close for b in bars])
        self.prepare(ctx)
        return ctx

    def prepare(self, ctx: Context) -> None:
        pass

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        raise NotImplementedError

    # helpers
    def _mk(self, ctx: Context, i: int, action: str, sl: float, tp: float, reason: str, inval: str,
            rules: tuple[str, ...]) -> Signal | None:
        ref = ctx.bars[i].close
        if action == "LONG" and not (sl < ref < tp):
            return None
        if action == "SHORT" and not (tp < ref < sl):
            return None
        rr = self.params.get("rr")
        rr_target = rr if (rr is not None and abs(tp - self._rr_target(action, ref, sl)) < 1e-9) else None
        return Signal(self.id, self.version, i, ctx.bars[i].open_time_utc.isoformat(), action, ref, sl, tp,
                      inval, reason, rules, rr_target)

    def _rr_target(self, action: str, ref: float, sl: float) -> float:
        r = abs(ref - sl) * self.params.get("rr", 2.0)
        return ref + r if action == "LONG" else ref - r

    @staticmethod
    def in_rollover(bar: Candle) -> bool:
        return bar.open_time_utc.hour in ROLLOVER_HOURS_UTC

    def describe(self) -> dict:
        return {"id": self.id, "version": self.version, "exec_tf": self.exec_tf.value,
                "htfs": [t.value for t in self.htfs], "params": self.params, "rules": self.rules.strip()}


def htf_ema_trend(ctx: Context, tf: Timeframe, fast: int, slow: int) -> list[str]:
    key = ("htf_ema", tf, fast, slow)
    if key not in ctx.cache:
        closes = [b.close for b in ctx.htf.get(tf, [])]
        f, s = ema(closes, fast), ema(closes, slow)
        out = []
        for a, b, c in zip(f, s, closes):
            if a is None or b is None:
                out.append("UNKNOWN")
            elif a > b and c > a:
                out.append("BULLISH")
            elif a < b and c < a:
                out.append("BEARISH")
            else:
                out.append("RANGE")
        ctx.cache[key] = out
    return ctx.cache[key]

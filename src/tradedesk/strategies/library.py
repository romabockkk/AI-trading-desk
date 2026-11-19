"""Six research strategies with formal, testable rules. None is claimed profitable."""
from __future__ import annotations

from ..analysis import structure as ms
from ..analysis.indicators import efficiency_ratio, ema, highest, lowest, rolling_mean_std
from ..schemas.market import Timeframe
from .base import Context, Signal, Strategy, htf_ema_trend


class SmcChochOrderBlock(Strategy):
    id = "SMC_CHOCH_OB"
    rules = """
    Context: H4 structure trend (MS.TREND.v1 on closed H4 bars) must be BULLISH for LONG / BEARISH for SHORT.
    Setup (M15): a liquidity sweep against the trend (MS.SWEEP.v1) followed within `sweep_to_break` bars by a
      CHoCH or BOS in the trend direction (MS.CHOCH.v1/MS.BOS.v1) that produced a valid order block (MS.OB.v1).
    Entry: first bar after the break, within `max_wait` bars, whose range touches the OB zone and closes
      back outside it in trade direction (LONG: low <= OB.high and close > OB.high). Enter next bar open.
    Stop: beyond the OB far side +/- `sl_atr` * ATR14.   Target: `rr` * risk.
    Invalidation: close through the OB far side before entry, or `max_wait` bars elapse.
    Filters: no entries during rollover hours 20-22 UTC.
    """
    default_params = {"k": 2, "sweep_to_break": 12, "max_wait": 16, "sl_atr": 0.3, "rr": 2.0, "disp_atr": 1.0}
    param_grid = {"rr": [2.0, 2.5], "max_wait": [8, 16]}

    def prepare(self, ctx: Context) -> None:
        p = self.params
        ctx.cache["ms"] = ms.analyze(ctx.bars, k=p["k"], displacement_atr=p["disp_atr"])
        ctx.cache["h4"] = ms.analyze(ctx.htf.get(Timeframe.H4, []), k=p["k"])
        r: ms.StructureResult = ctx.cache["ms"]
        # index OB by the break bar
        ctx.cache["ob_by_break"] = {ob.index: ob for ob in r.order_blocks}

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        if self.in_rollover(bar) or ctx.atr14[i] is None:
            return None
        h4i = ctx.htf_index(Timeframe.H4, i)
        h4_trend = ctx.cache["h4"].trend_at(h4i) if h4i >= 0 else "UNKNOWN"
        if h4_trend not in ("BULLISH", "BEARISH"):
            return None
        r: ms.StructureResult = ctx.cache["ms"]
        for brk in reversed(r.breaks):
            if brk.index >= i:
                continue
            if i - brk.index > p["max_wait"]:
                break
            if brk.direction != h4_trend:
                continue
            ob = ctx.cache["ob_by_break"].get(brk.index)
            if ob is None or (ob.invalidated_at is not None and ob.invalidated_at <= i):
                continue
            want_sweep = "BULLISH" if brk.direction == "BULLISH" else "BEARISH"
            swept = any(s.direction == want_sweep and 0 <= brk.index - s.index <= p["sweep_to_break"]
                        for s in r.sweeps)
            if not swept:
                continue
            # first touch only: no earlier touch between break and i
            def touched(j: int) -> bool:
                b = ctx.bars[j]
                if brk.direction == "BULLISH":
                    return b.low <= ob.high and b.close > ob.high
                return b.high >= ob.low and b.close < ob.low
            if not touched(i) or any(touched(j) for j in range(brk.index + 1, i)):
                continue
            a = ctx.atr14[i]
            if brk.direction == "BULLISH":
                sl = ob.low - p["sl_atr"] * a
                return self._mk(ctx, i, "LONG", sl, self._rr_target("LONG", bar.close, sl),
                                f"H4 bullish; sweep then {brk.kind} up at bar {brk.index}; OB retest",
                                f"close below {ob.low:.2f}", ("MS.TREND.v1", "MS.SWEEP.v1", f"MS.{brk.kind}.v1", "MS.OB.v1"))
            sl = ob.high + p["sl_atr"] * a
            return self._mk(ctx, i, "SHORT", sl, self._rr_target("SHORT", bar.close, sl),
                            f"H4 bearish; sweep then {brk.kind} down at bar {brk.index}; OB retest",
                            f"close above {ob.high:.2f}", ("MS.TREND.v1", "MS.SWEEP.v1", f"MS.{brk.kind}.v1", "MS.OB.v1"))
        return None


class TrendContinuation(Strategy):
    id = "TREND_CONTINUATION"
    rules = """
    Regime: H1 EMA(50) vs EMA(200) with close beyond EMA(50) => BULLISH / BEARISH (else no trade);
      M15 efficiency ratio ER(20) >= `min_er`.
    Setup: pullback — some bar in the last 3 M15 bars traded through EMA(20) (LONG: low <= EMA20).
    Entry: current bar closes back in trend direction beyond EMA(20) and beyond its open. Next bar open.
    Stop: lowest low (LONG) / highest high (SHORT) of last `swing_bars` bars -/+ 0.2*ATR14. Target: `rr` * risk.
    Invalidation: H1 regime flips. Filters: no rollover hours.
    """
    default_params = {"min_er": 0.25, "swing_bars": 6, "rr": 2.0}
    param_grid = {"min_er": [0.2, 0.3], "rr": [2.0, 2.5]}

    def prepare(self, ctx: Context) -> None:
        ctx.cache["ema20"] = ema(ctx.closes, 20)

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        e = ctx.cache["ema20"]
        if i < 25 or self.in_rollover(bar) or e[i] is None or ctx.atr14[i] is None:
            return None
        hi = ctx.htf_index(Timeframe.H1, i)
        trend = htf_ema_trend(ctx, Timeframe.H1, 50, 200)
        reg = trend[hi] if hi >= 0 else "UNKNOWN"
        er = efficiency_ratio(ctx.closes, 20, i)
        if reg not in ("BULLISH", "BEARISH") or er is None or er < p["min_er"]:
            return None
        a = ctx.atr14[i]
        if reg == "BULLISH":
            if any(ctx.bars[j].low <= e[j] for j in range(i - 3, i)) and bar.close > e[i] and bar.close > bar.open:
                sl = lowest([b.low for b in ctx.bars], p["swing_bars"], i + 1) - 0.2 * a
                return self._mk(ctx, i, "LONG", sl, self._rr_target("LONG", bar.close, sl),
                                f"H1 uptrend, ER={er:.2f}, EMA20 pullback rejected", "H1 regime not BULLISH",
                                ("TC.REGIME.v1", "TC.PULLBACK.v1"))
        else:
            if any(ctx.bars[j].high >= e[j] for j in range(i - 3, i)) and bar.close < e[i] and bar.close < bar.open:
                sl = highest([b.high for b in ctx.bars], p["swing_bars"], i + 1) + 0.2 * a
                return self._mk(ctx, i, "SHORT", sl, self._rr_target("SHORT", bar.close, sl),
                                f"H1 downtrend, ER={er:.2f}, EMA20 pullback rejected", "H1 regime not BEARISH",
                                ("TC.REGIME.v1", "TC.PULLBACK.v1"))
        return None


class BreakoutRetest(Strategy):
    id = "BREAKOUT_RETEST"
    rules = """
    Level: Donchian high/low of the previous `n` M15 bars (excluding the breakout bar).
    Breakout: a close beyond the level by >= 0.2*ATR14 at bar b.
    Retest: within `retest_window` bars after b, a bar trades back to within `tol_atr`*ATR of the level
      and closes on the breakout side. Entry next bar open. Only the first retest counts.
    Stop: level -/+ `sl_atr`*ATR14. Target: `rr` * risk. Invalidation: close back through level by > sl distance.
    Filters: H4 EMA(20/50) regime must not oppose the breakout; no rollover hours.
    """
    default_params = {"n": 48, "retest_window": 12, "tol_atr": 0.3, "sl_atr": 1.0, "rr": 2.0}
    param_grid = {"n": [32, 64], "sl_atr": [0.8, 1.2]}

    def prepare(self, ctx: Context) -> None:
        n = self.params["n"]
        highs = [b.high for b in ctx.bars]
        lows = [b.low for b in ctx.bars]
        brks = []   # (index, dir, level)
        for i in range(n, len(ctx.bars)):
            a = ctx.atr14[i]
            if a is None:
                continue
            hh, ll = highest(highs, n, i), lowest(lows, n, i)
            c = ctx.bars[i].close
            if c > hh + 0.2 * a:
                brks.append((i, "LONG", hh))
            elif c < ll - 0.2 * a:
                brks.append((i, "SHORT", ll))
        ctx.cache["brks"] = brks

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        a = ctx.atr14[i]
        if self.in_rollover(bar) or a is None:
            return None
        h4 = htf_ema_trend(ctx, Timeframe.H4, 20, 50)
        hi = ctx.htf_index(Timeframe.H4, i)
        reg = h4[hi] if hi >= 0 else "UNKNOWN"
        for (b, d, lvl) in reversed(ctx.cache["brks"]):
            if b >= i:
                continue
            if i - b > p["retest_window"]:
                break
            if (d == "LONG" and reg == "BEARISH") or (d == "SHORT" and reg == "BULLISH"):
                continue

            def is_retest(j: int) -> bool:
                x = ctx.bars[j]
                aj = ctx.atr14[j] or a
                if d == "LONG":
                    return x.low <= lvl + p["tol_atr"] * aj and x.close > lvl
                return x.high >= lvl - p["tol_atr"] * aj and x.close < lvl
            if is_retest(i) and not any(is_retest(j) for j in range(b + 1, i)):
                sl = lvl - p["sl_atr"] * a if d == "LONG" else lvl + p["sl_atr"] * a
                return self._mk(ctx, i, d, sl, self._rr_target(d, bar.close, sl),
                                f"{p['n']}-bar breakout at bar {b}, first retest of {lvl:.2f}",
                                f"close back through {lvl:.2f}", ("BR.LEVEL.v1", "BR.RETEST.v1"))
        return None


class LondonSessionBreakout(Strategy):
    id = "SESSION_LONDON_BREAKOUT"
    rules = """
    Range: Asian session high/low from M15 bars with open time 00:00-06:45 UTC of the same day.
    Range filter: width between `min_w`*ATR14 and `max_w`*ATR14 (ATR at 07:00 UTC bar).
    Entry: first M15 close above range high (LONG) or below range low (SHORT) between 07:00 and 10:45 UTC.
      One signal per day. Next bar open.
    Stop: range midpoint. Target: `rr` * risk. Invalidation: close back inside the range beyond midpoint.
    Note: session times are UTC and ignore DST shifts of London — a known simplification to test.
    """
    default_params = {"min_w": 1.0, "max_w": 6.0, "rr": 2.0}
    param_grid = {"max_w": [4.0, 8.0], "rr": [1.5, 2.0]}

    def prepare(self, ctx: Context) -> None:
        days: dict = {}
        for i, b in enumerate(ctx.bars):
            d = b.open_time_utc.date()
            if b.open_time_utc.hour < 7:
                r = days.setdefault(d, {"hi": b.high, "lo": b.low, "n": 0})
                r["hi"], r["lo"], r["n"] = max(r["hi"], b.high), min(r["lo"], b.low), r["n"] + 1
        ctx.cache["asia"] = days
        ctx.cache["fired"] = set()

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        t = bar.open_time_utc
        if not (7 <= t.hour < 11) or ctx.atr14[i] is None:
            return None
        d = t.date()
        rng = ctx.cache["asia"].get(d)
        if not rng or rng["n"] < 16:
            return None
        # one signal per day: check earlier bars today deterministically (no mutable state dependence)
        def breaks(j: int) -> str | None:
            x = ctx.bars[j]
            if x.close > rng["hi"]:
                return "LONG"
            if x.close < rng["lo"]:
                return "SHORT"
            return None
        j0 = i
        while j0 - 1 >= 0 and ctx.bars[j0 - 1].open_time_utc.date() == d and ctx.bars[j0 - 1].open_time_utc.hour >= 7:
            j0 -= 1
        if any(breaks(j) for j in range(j0, i)):
            return None
        dirn = breaks(i)
        if dirn is None:
            return None
        w = rng["hi"] - rng["lo"]
        a = ctx.atr14[j0] or ctx.atr14[i]
        if not (p["min_w"] * a <= w <= p["max_w"] * a):
            return None
        mid = (rng["hi"] + rng["lo"]) / 2
        return self._mk(ctx, i, dirn, mid, self._rr_target(dirn, bar.close, mid),
                        f"London breakout of Asian range {rng['lo']:.2f}-{rng['hi']:.2f}",
                        f"close beyond midpoint {mid:.2f}", ("SES.ASIA_RANGE.v1", "SES.LONDON_BREAK.v1"))


class VolatilityBreakout(Strategy):
    id = "VOLATILITY_BREAKOUT"
    rules = """
    Squeeze: Bollinger band width (20, 2σ) of the previous bar is in the lowest `pct` fraction of the
      previous `lookback` widths.
    Trigger: current bar closes outside the band with range >= `range_atr`*ATR14. Direction = side of close.
    Stop: Bollinger middle band (SMA20) of the trigger bar. Target: `rr` * risk.
    Invalidation: close back inside the bands beyond the middle band. Filters: no rollover hours.
    """
    default_params = {"lookback": 120, "pct": 0.2, "range_atr": 1.2, "rr": 2.0}
    param_grid = {"pct": [0.15, 0.25], "range_atr": [1.0, 1.4]}

    def prepare(self, ctx: Context) -> None:
        m, s = rolling_mean_std(ctx.closes, 20)
        ctx.cache["bb"] = (m, s)
        ctx.cache["width"] = [None if mm is None else (4 * ss) / mm for mm, ss in zip(m, s)]

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        m, s = ctx.cache["bb"]
        w = ctx.cache["width"]
        if i < p["lookback"] + 21 or self.in_rollover(bar) or m[i] is None or ctx.atr14[i] is None:
            return None
        hist = [x for x in w[i - 1 - p["lookback"]:i - 1] if x is not None]
        if not hist or w[i - 1] is None:
            return None
        rank = sum(1 for x in hist if x < w[i - 1]) / len(hist)
        if rank > p["pct"] or (bar.high - bar.low) < p["range_atr"] * ctx.atr14[i]:
            return None
        up, dn = m[i] + 2 * s[i], m[i] - 2 * s[i]
        if bar.close > up:
            return self._mk(ctx, i, "LONG", m[i], self._rr_target("LONG", bar.close, m[i]),
                            f"squeeze rank {rank:.2f}, expansion up", f"close below {m[i]:.2f}", ("VB.SQUEEZE.v1",))
        if bar.close < dn:
            return self._mk(ctx, i, "SHORT", m[i], self._rr_target("SHORT", bar.close, m[i]),
                            f"squeeze rank {rank:.2f}, expansion down", f"close above {m[i]:.2f}", ("VB.SQUEEZE.v1",))
        return None


class MeanReversion(Strategy):
    id = "MEAN_REVERSION"
    rules = """
    Regime: ranging only — M15 ER(50) <= `max_er` and H4 EMA(20/50) regime is RANGE or UNKNOWN.
    Trigger: z-score of close vs SMA(40) <= -`z` (LONG) or >= +`z` (SHORT) and the bar closes in the
      reversal direction (LONG: close > open).
    Stop: bar extreme -/+ `sl_atr`*ATR14. Target: SMA(40) at signal time (mean).
    Signal is discarded when planned RR < `min_rr` (the Risk Gate also enforces its own minimum).
    Invalidation: new extreme beyond the stop. Filters: no rollover hours.
    """
    default_params = {"max_er": 0.2, "z": 2.2, "sl_atr": 0.5, "min_rr": 2.0}
    param_grid = {"z": [2.0, 2.5], "sl_atr": [0.3, 0.6]}

    def prepare(self, ctx: Context) -> None:
        ctx.cache["ms40"] = rolling_mean_std(ctx.closes, 40)

    def signal_at(self, ctx: Context, i: int) -> Signal | None:
        p = self.params
        bar = ctx.bars[i]
        m, s = ctx.cache["ms40"]
        if i < 60 or self.in_rollover(bar) or m[i] is None or not s[i] or ctx.atr14[i] is None:
            return None
        er = efficiency_ratio(ctx.closes, 50, i)
        h4 = htf_ema_trend(ctx, Timeframe.H4, 20, 50)
        hi = ctx.htf_index(Timeframe.H4, i)
        if er is None or er > p["max_er"] or (hi >= 0 and h4[hi] in ("BULLISH", "BEARISH")):
            return None
        z = (bar.close - m[i]) / s[i]
        a = ctx.atr14[i]
        sig = None
        if z <= -p["z"] and bar.close > bar.open:
            sig = self._mk(ctx, i, "LONG", bar.low - p["sl_atr"] * a, m[i], f"range regime ER={er:.2f}, z={z:.2f}",
                           "new low below stop", ("MR.REGIME.v1", "MR.ZSCORE.v1"))
        elif z >= p["z"] and bar.close < bar.open:
            sig = self._mk(ctx, i, "SHORT", bar.high + p["sl_atr"] * a, m[i], f"range regime ER={er:.2f}, z={z:.2f}",
                           "new high above stop", ("MR.REGIME.v1", "MR.ZSCORE.v1"))
        if sig and sig.planned_rr < p["min_rr"]:
            return None
        return sig


ALL_STRATEGIES: dict[str, type[Strategy]] = {
    s.id: s for s in (SmcChochOrderBlock, TrendContinuation, BreakoutRetest, LondonSessionBreakout,
                      VolatilityBreakout, MeanReversion)
}


def build(strategy_id: str, params: dict | None = None) -> Strategy:
    if strategy_id not in ALL_STRATEGIES:
        raise KeyError(f"unknown strategy {strategy_id}")
    return ALL_STRATEGIES[strategy_id](**(params or {}))

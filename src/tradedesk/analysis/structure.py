"""Formal market-structure definitions (rule ids are referenced in agent reports).

All detections are causal. A swing at bar i is only *confirmed* at bar i+k, and is
reported with `confirmed_at = i+k`; consumers must ignore it before that index.

Rules
- MS.SWING.v1  Swing high at i: high[i] > high[j] for all j in [i-k, i+k], j != i
               (strict on the left, >= on the right ties broken towards the earlier bar).
               Swing low symmetric. Confirmed at i+k.
- MS.LABEL.v1  Each confirmed swing high is HH if > previous confirmed swing high else LH;
               swing low is HL if > previous swing low else LL.
- MS.TREND.v1  Trend BULLISH after a BOS/CHoCH up, BEARISH after one down, UNKNOWN before the first.
- MS.BOS.v1    In a BULLISH trend: first close above the last confirmed swing high => BOS up.
               In a BEARISH trend: first close below the last confirmed swing low => BOS down.
- MS.CHOCH.v1  In a BULLISH trend: first close below the last confirmed swing low => CHoCH down
               (trend flips BEARISH). Symmetric for BEARISH. From UNKNOWN, the first break in
               either direction is labelled BOS and sets the trend.
- MS.OB.v1     On a BOS/CHoCH up at bar b: the bullish order block is the last bearish candle
               (close < open) in [swing_index, b]; zone = [low, high] of that candle. It requires
               displacement: range of the breaking leg >= displacement_atr * ATR(14).
               Symmetric for bearish. Invalidated when a later close goes through the far side.
- MS.FVG.v1    Bullish FVG at bar i (3-candle pattern i-2,i-1,i): low[i] > high[i-2];
               zone = [high[i-2], low[i]]. Bearish: high[i] < low[i-2]. Known at bar i.
- MS.SWEEP.v1  Bullish liquidity sweep at bar i: low[i] < last confirmed swing low L and
               close[i] > L. Bearish: high[i] > last swing high H and close[i] < H.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas.market import Candle
from .indicators import atr


@dataclass(frozen=True)
class Swing:
    index: int
    confirmed_at: int
    kind: str          # HIGH | LOW
    price: float
    label: str = ""    # HH/LH/HL/LL or "" for the first


@dataclass(frozen=True)
class StructureBreak:
    index: int
    kind: str          # BOS | CHOCH
    direction: str     # BULLISH | BEARISH
    level: float
    swing_index: int


@dataclass(frozen=True)
class Zone:
    index: int          # bar index where the zone becomes known
    kind: str           # ORDER_BLOCK | FVG
    direction: str
    low: float
    high: float
    origin_index: int
    invalidated_at: int | None = None


@dataclass(frozen=True)
class Sweep:
    index: int
    direction: str
    level: float


@dataclass
class StructureResult:
    swings: list[Swing] = field(default_factory=list)
    breaks: list[StructureBreak] = field(default_factory=list)
    order_blocks: list[Zone] = field(default_factory=list)
    fvgs: list[Zone] = field(default_factory=list)
    sweeps: list[Sweep] = field(default_factory=list)
    trend: list[str] = field(default_factory=list)   # trend state after each bar

    def trend_at(self, i: int) -> str:
        return self.trend[i] if 0 <= i < len(self.trend) else "UNKNOWN"


def find_swings(c: list[Candle], k: int = 2) -> list[Swing]:
    raw: list[Swing] = []
    for i in range(k, len(c) - k):
        h = c[i].high
        if all(h > c[j].high for j in range(i - k, i)) and all(h >= c[j].high for j in range(i + 1, i + k + 1)):
            raw.append(Swing(i, i + k, "HIGH", h))
        lo = c[i].low
        if all(lo < c[j].low for j in range(i - k, i)) and all(lo <= c[j].low for j in range(i + 1, i + k + 1)):
            raw.append(Swing(i, i + k, "LOW", lo))
    raw.sort(key=lambda s: (s.confirmed_at, s.index))
    labelled = []
    last = {"HIGH": None, "LOW": None}
    for s in raw:
        p = last[s.kind]
        if p is None:
            lab = ""
        elif s.kind == "HIGH":
            lab = "HH" if s.price > p.price else "LH"
        else:
            lab = "HL" if s.price > p.price else "LL"
        ls = Swing(s.index, s.confirmed_at, s.kind, s.price, lab)
        labelled.append(ls)
        last[s.kind] = ls
    return labelled


def analyze(c: list[Candle], k: int = 2, displacement_atr: float = 1.0) -> StructureResult:
    res = StructureResult()
    if len(c) < 2 * k + 3:
        res.trend = ["UNKNOWN"] * len(c)
        return res
    swings = find_swings(c, k)
    res.swings = swings
    a = atr(c, 14)
    by_conf: dict[int, list[Swing]] = {}
    for s in swings:
        by_conf.setdefault(s.confirmed_at, []).append(s)
    last_high: Swing | None = None
    last_low: Swing | None = None
    high_broken = low_broken = False
    trend = "UNKNOWN"
    active_obs: list[Zone] = []
    for i in range(len(c)):
        for s in by_conf.get(i, []):
            if s.kind == "HIGH":
                last_high, high_broken = s, False
            else:
                last_low, low_broken = s, False
        x = c[i]
        # sweeps (wick through, close back inside)
        if last_low and x.low < last_low.price < x.close:
            res.sweeps.append(Sweep(i, "BULLISH", last_low.price))
        if last_high and x.high > last_high.price > x.close:
            res.sweeps.append(Sweep(i, "BEARISH", last_high.price))
        # FVG
        if i >= 2:
            if x.low > c[i - 2].high:
                res.fvgs.append(Zone(i, "FVG", "BULLISH", c[i - 2].high, x.low, i - 1))
            elif x.high < c[i - 2].low:
                res.fvgs.append(Zone(i, "FVG", "BEARISH", x.high, c[i - 2].low, i - 1))
        # structure breaks on close
        brk = None
        if last_high and not high_broken and x.close > last_high.price:
            kind = "CHOCH" if trend == "BEARISH" else "BOS"
            brk = StructureBreak(i, kind, "BULLISH", last_high.price, last_high.index)
            high_broken = True
            trend = "BULLISH"
        elif last_low and not low_broken and x.close < last_low.price:
            kind = "CHOCH" if trend == "BULLISH" else "BOS"
            brk = StructureBreak(i, kind, "BEARISH", last_low.price, last_low.index)
            low_broken = True
            trend = "BEARISH"
        if brk:
            res.breaks.append(brk)
            ob = _order_block(c, brk, a, displacement_atr)
            if ob:
                res.order_blocks.append(ob)
                active_obs.append(ob)
        # OB invalidation
        still = []
        for ob in active_obs:
            if (ob.direction == "BULLISH" and x.close < ob.low) or (ob.direction == "BEARISH" and x.close > ob.high):
                idx = res.order_blocks.index(ob)
                res.order_blocks[idx] = Zone(ob.index, ob.kind, ob.direction, ob.low, ob.high, ob.origin_index, i)
            else:
                still.append(ob)
        active_obs = still
        res.trend.append(trend)
    return res


def _order_block(c: list[Candle], b: StructureBreak, a: list[float | None], disp: float) -> Zone | None:
    lo_i = b.swing_index
    leg = c[lo_i:b.index + 1]
    atr_v = a[b.index]
    if not leg or atr_v is None:
        return None
    rng = max(x.high for x in leg) - min(x.low for x in leg)
    if rng < disp * atr_v:
        return None
    want_bear = b.direction == "BULLISH"
    for j in range(b.index, lo_i - 1, -1):
        x = c[j]
        if (want_bear and x.close < x.open) or (not want_bear and x.close > x.open):
            return Zone(b.index, "ORDER_BLOCK", b.direction, x.low, x.high, j)
    return None


def bias(res: StructureResult, i: int) -> str:
    t = res.trend_at(i)
    return t if t in ("BULLISH", "BEARISH") else "UNKNOWN"

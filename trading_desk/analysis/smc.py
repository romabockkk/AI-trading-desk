"""Objective Smart-Money-Concepts rules for STRATEGY.md v1.0.

All functions are pure and look only at *closed* candles that are passed in, so the
exact same code runs in live cycles and in the backtester (no look-ahead).

Definitions used (kept deliberately simple and testable):
* Swing high/low: fractal pivot, ``n`` bars on each side. A pivot at index i is only
  known once bar i+n has closed.
* H4 bias: last two swing highs AND last two swing lows rising -> BULLISH, both
  falling -> BEARISH, anything else -> RANGE (no trade). A close beyond the latest
  opposite swing (structure break against the bias) cancels the bias.
* Liquidity sweep (H1): wick through a prior confirmed swing low (high) and close back
  above (below) it, with no later H1 close beyond the level.
* CHoCH (M15): first close above (below) the last M15 swing high (low) that was
  confirmed before the sweep extreme.
* Zone: last FVG in the displacement leg, otherwise the last opposite candle (Order
  Block) before the displacement. Entry = zone midpoint.
* SL: beyond the sweep extreme + ATR buffer. TP: first opposing H1 liquidity at
  >= min_rr, capped at max_rr; if none, min_rr.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    if len(c) < period + 1:
        return float("nan")
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return float(tr[-period:].mean())


def swing_points(values: np.ndarray, n: int, kind: str) -> list[int]:
    """Indices of confirmed fractal pivots (kind='high' or 'low').

    Pivot i: extreme of the window [i-n, i+n] and strictly beyond the n bars to its
    right (so equal highs/lows produce one pivot, the first).
    """
    values = np.asarray(values, dtype=float)
    if len(values) < 2 * n + 1:
        return []
    win = np.lib.stride_tricks.sliding_window_view(values, 2 * n + 1)
    centre = win[:, n]
    right = win[:, n + 1:]
    if kind == "high":
        mask = (centre == win.max(axis=1)) & (right < centre[:, None]).all(axis=1)
    else:
        mask = (centre == win.min(axis=1)) & (right > centre[:, None]).all(axis=1)
    return (np.nonzero(mask)[0] + n).tolist()


@dataclass
class Bias:
    direction: str               # BULLISH | BEARISH | RANGE
    detail: dict[str, Any] = field(default_factory=dict)


def h4_bias(h4: pd.DataFrame, n: int) -> Bias:
    hi, lo, cl = h4["high"].to_numpy(), h4["low"].to_numpy(), h4["close"].to_numpy()
    sh, sl = swing_points(hi, n, "high"), swing_points(lo, n, "low")
    if len(sh) < 2 or len(sl) < 2:
        return Bias("RANGE", {"reason": "not enough H4 swings"})
    h1_, h2_ = hi[sh[-2]], hi[sh[-1]]
    l1_, l2_ = lo[sl[-2]], lo[sl[-1]]
    detail = {"swing_highs": [float(h1_), float(h2_)], "swing_lows": [float(l1_), float(l2_)],
              "last_close": float(cl[-1])}
    if h2_ > h1_ and l2_ > l1_:
        if cl[-1] < l2_:
            return Bias("RANGE", {**detail, "reason": "bullish structure broken (close below last HL)"})
        return Bias("BULLISH", {**detail, "reason": "HH + HL"})
    if h2_ < h1_ and l2_ < l1_:
        if cl[-1] > h2_:
            return Bias("RANGE", {**detail, "reason": "bearish structure broken (close above last LH)"})
        return Bias("BEARISH", {**detail, "reason": "LH + LL"})
    return Bias("RANGE", {**detail, "reason": "mixed structure"})


@dataclass
class Setup:
    status: str                  # NONE | FORMING | READY | STALE | INVALID
    direction: str | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _ts(index: pd.DatetimeIndex, i: int) -> str:
    return index[i].isoformat()


def detect_setup(m15: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, p) -> Setup:
    """Run the full v1.0 rule set. ``p`` is a StrategyParams."""
    bias = h4_bias(h4, p.swing_lookback)
    if bias.direction == "RANGE":
        return Setup("NONE", details={"h4_bias": bias.direction, **bias.detail},
                     notes=[f"H4 structure unclear: {bias.detail.get('reason')}"])
    long = bias.direction == "BULLISH"
    sign = 1 if long else -1
    n = p.swing_lookback
    base = {"h4_bias": bias.direction, "h4": bias.detail}

    # ---- 1) H1 liquidity sweep -------------------------------------------------
    hh, hl, hc = h1["high"].to_numpy(), h1["low"].to_numpy(), h1["close"].to_numpy()
    pivots = swing_points(hl if long else hh, n, "low" if long else "high")
    start = max(0, len(h1) - p.sweep_window_h1)
    sweep = None
    for j in range(len(h1) - 1, start - 1, -1):
        wick, close = (hl[j], hc[j]) if long else (hh[j], hc[j])
        # candidate levels: swings confirmed before bar j (pivot index + n < j)
        levels = [(k, (hl if long else hh)[k]) for k in pivots if k + n < j]
        for k, lvl in reversed(levels[-6:]):
            took = wick < lvl if long else wick > lvl
            reclaimed = close > lvl if long else close < lvl
            if took and reclaimed:
                later = hc[j + 1:]
                if (later < lvl).any() if long else (later > lvl).any():
                    continue  # accepted beyond the level -> not a sweep
                sweep = (j, k, float(lvl))
                break
        if sweep:
            break
    if not sweep:
        return Setup("NONE", "LONG" if long else "SHORT", details=base,
                     notes=["no qualifying H1 liquidity sweep in window"])
    j, k, lvl = sweep
    sweep_time = h1.index[j]
    base["sweep"] = {"h1_bar": _ts(h1.index, j), "level": lvl, "level_from": _ts(h1.index, k),
                     "side": "sell-side (lows)" if long else "buy-side (highs)"}

    # ---- 2) M15 CHoCH after the sweep extreme ----------------------------------
    mh, ml, mc, mo = (m15[c].to_numpy() for c in ("high", "low", "close", "open"))
    after = np.where(m15.index >= sweep_time)[0]
    if len(after) == 0:
        return Setup("FORMING", "LONG" if long else "SHORT", details=base, notes=["sweep found, waiting for M15 data"])
    seg = after
    ext_i = int(seg[np.argmin(ml[seg])]) if long else int(seg[np.argmax(mh[seg])])
    extreme = float(ml[ext_i] if long else mh[ext_i])
    ref_pivots = swing_points(mh if long else ml, n, "high" if long else "low")
    refs = [q for q in ref_pivots if q + n <= ext_i]
    if not refs:
        return Setup("FORMING", "LONG" if long else "SHORT", details=base, notes=["no M15 reference swing for CHoCH"])
    ref_i = refs[-1]
    ref_lvl = float(mh[ref_i] if long else ml[ref_i])
    choch_i = None
    for q in range(ext_i + 1, min(len(m15), ext_i + 1 + p.confirm_window_m15)):
        if (mc[q] > ref_lvl) if long else (mc[q] < ref_lvl):
            choch_i = q
            break
    base["m15"] = {"sweep_extreme": extreme, "extreme_bar": _ts(m15.index, ext_i),
                   "choch_level": ref_lvl, "choch_ref_bar": _ts(m15.index, ref_i)}
    if choch_i is None:
        if len(m15) - 1 - ext_i > p.confirm_window_m15:
            return Setup("STALE", "LONG" if long else "SHORT", details=base, notes=["no CHoCH within confirmation window"])
        return Setup("FORMING", "LONG" if long else "SHORT", details=base,
                     notes=[f"sweep confirmed, waiting for M15 close {'above' if long else 'below'} {ref_lvl:g}"])
    base["m15"]["choch_bar"] = _ts(m15.index, choch_i)

    # ---- 3) Zone: FVG in the displacement leg, else Order Block ---------------
    zone = None
    for q in range(choch_i, ext_i + 1, -1):
        if q - 2 < 0:
            break
        if long and ml[q] > mh[q - 2]:
            zone = ("FVG", float(mh[q - 2]), float(ml[q]), q)
            break
        if not long and mh[q] < ml[q - 2]:
            zone = ("FVG", float(mh[q]), float(ml[q - 2]), q)
            break
    if zone is None:
        for q in range(ext_i, max(-1, ext_i - 6), -1):
            opposite = mc[q] < mo[q] if long else mc[q] > mo[q]
            if opposite:
                zone = ("ORDER_BLOCK", float(ml[q]), float(mh[q]), q)
                break
    if zone is None:
        return Setup("INVALID", "LONG" if long else "SHORT", details=base, notes=["no FVG / order block in displacement leg"])
    ztype, zlo, zhi, zi = zone
    base["zone"] = {"type": ztype, "low": zlo, "high": zhi, "bar": _ts(m15.index, zi)}

    # ---- 4) Levels ----------------------------------------------------------
    a = atr(m15)
    entry = (zlo + zhi) / 2
    sl = extreme - sign * p.sl_atr_buffer * a
    risk = (entry - sl) * sign
    if not np.isfinite(risk) or risk <= 0:
        return Setup("INVALID", "LONG" if long else "SHORT", details=base, notes=["stop loss cannot be defined"])

    liq_idx = swing_points(hh if long else hl, n, "high" if long else "low")
    liq = sorted({float((hh if long else hl)[q]) for q in liq_idx if ((hh if long else hl)[q] - entry) * sign > 0},
                 key=lambda x: (x - entry) * sign)
    min_tp = entry + sign * p.min_rr * risk
    obstacles = [x for x in liq if (x - min_tp) * sign < 0]
    beyond = [x for x in liq if (x - min_tp) * sign >= 0]
    cap = entry + sign * p.max_rr_target * risk
    if beyond:
        tp = beyond[0] if (beyond[0] - cap) * sign <= 0 else cap
    else:
        tp = min_tp
    base["levels"] = {"atr_m15": a, "risk_per_unit": risk, "opposing_liquidity": liq[:5],
                      "liquidity_obstacles_before_min_rr": obstacles}

    # ---- 5) Freshness / invalidation since the CHoCH -------------------------
    post = slice(choch_i + 1, len(m15))
    bars_since = len(m15) - 1 - choch_i
    if (ml[post] <= sl).any() if long else (mh[post] >= sl).any():
        return Setup("INVALID", "LONG" if long else "SHORT", entry, sl, tp, base, ["price already hit the stop level"])
    if (mh[post] >= tp).any() if long else (ml[post] <= tp).any():
        return Setup("STALE", "LONG" if long else "SHORT", entry, sl, tp, base, ["price already reached target"])
    touched = (ml[post] <= entry).any() if long else (mh[post] >= entry).any()
    if touched:
        return Setup("STALE", "LONG" if long else "SHORT", entry, sl, tp, base, ["entry zone already traded after CHoCH"])
    if bars_since > p.entry_valid_bars_m15:
        return Setup("STALE", "LONG" if long else "SHORT", entry, sl, tp, base,
                     [f"CHoCH is {bars_since} M15 bars old (limit {p.entry_valid_bars_m15})"])
    base["bars_since_choch"] = bars_since
    return Setup("READY", "LONG" if long else "SHORT", float(entry), float(sl), float(tp), base,
                 [f"{ztype} retest entry after H1 sweep of {lvl:g} and M15 CHoCH through {ref_lvl:g}"])

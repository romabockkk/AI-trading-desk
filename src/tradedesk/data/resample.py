"""Aggregate M5 candles into higher timeframes using UTC-aligned buckets.

A higher-timeframe candle is marked closed only when its bucket end <= as_of.
Partial (still forming) buckets are returned with is_closed=False so callers can
exclude them; strategies must only use closed candles (no look-ahead).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..schemas.market import Candle, Timeframe


def _bucket(t: datetime, tf: Timeframe) -> datetime:
    epoch = int(t.timestamp())
    return datetime.fromtimestamp(epoch - epoch % tf.seconds, tz=t.tzinfo)


def resample(m5: list[Candle], tf: Timeframe, as_of: datetime, include_open: bool = False) -> list[Candle]:
    if m5 and m5[0].timeframe is tf:
        return [c for c in m5 if c.open_time_utc + timedelta(seconds=tf.seconds) <= as_of]
    out: list[Candle] = []
    cur: dict | None = None
    for c in m5:
        b = _bucket(c.open_time_utc, tf)
        if cur is None or cur["t"] != b:
            if cur is not None:
                out.append(cur)
            cur = {"t": b, "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.tick_volume,
                   "s": c.spread_points, "sp": c.spread_price}
        else:
            cur["h"] = max(cur["h"], c.high)
            cur["l"] = min(cur["l"], c.low)
            cur["c"] = c.close
            cur["v"] += c.tick_volume
            if c.spread_points is not None:
                cur["s"] = max(cur["s"] or 0, c.spread_points)
            if c.spread_price is not None:
                cur["sp"] = max(cur["sp"] or 0, c.spread_price)
    if cur is not None:
        out.append(cur)
    sym = m5[0].symbol if m5 else ""
    res = []
    for d in out:
        closed = d["t"] + timedelta(seconds=tf.seconds) <= as_of
        if not closed and not include_open:
            continue
        res.append(Candle(symbol=sym, timeframe=tf, open_time_utc=d["t"], open=d["o"], high=d["h"], low=d["l"],
                          close=d["c"], tick_volume=d["v"], spread_points=d["s"], spread_price=d["sp"],
                          is_closed=closed))
    return res

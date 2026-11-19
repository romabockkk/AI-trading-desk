"""Causal indicators: value at index i uses only bars 0..i."""
from __future__ import annotations

import math

from ..schemas.market import Candle


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2 / (period + 1)
    e = None
    for i, v in enumerate(values):
        if i < period - 1:
            out.append(None)
            continue
        if e is None:
            e = sum(values[i - period + 1:i + 1]) / period
        else:
            e = v * k + e * (1 - k)
        out.append(e)
    return out


def true_ranges(c: list[Candle]) -> list[float]:
    tr = []
    for i, x in enumerate(c):
        if i == 0:
            tr.append(x.high - x.low)
        else:
            pc = c[i - 1].close
            tr.append(max(x.high - x.low, abs(x.high - pc), abs(x.low - pc)))
    return tr


def atr(c: list[Candle], period: int = 14) -> list[float | None]:
    tr = true_ranges(c)
    out: list[float | None] = []
    a = None
    for i, v in enumerate(tr):
        if i < period - 1:
            out.append(None)
            continue
        a = sum(tr[i - period + 1:i + 1]) / period if a is None else (a * (period - 1) + v) / period
        out.append(a)
    return out


def rolling_mean_std(values: list[float], period: int) -> tuple[list[float | None], list[float | None]]:
    m, s = [], []
    for i in range(len(values)):
        if i < period - 1:
            m.append(None)
            s.append(None)
            continue
        w = values[i - period + 1:i + 1]
        mu = sum(w) / period
        m.append(mu)
        s.append(math.sqrt(sum((x - mu) ** 2 for x in w) / period))
    return m, s


def highest(values: list[float], period: int, end_exclusive: int) -> float | None:
    if end_exclusive - period < 0:
        return None
    return max(values[end_exclusive - period:end_exclusive])


def lowest(values: list[float], period: int, end_exclusive: int) -> float | None:
    if end_exclusive - period < 0:
        return None
    return min(values[end_exclusive - period:end_exclusive])


def efficiency_ratio(closes: list[float], period: int, i: int) -> float | None:
    """Kaufman efficiency ratio in [0,1]: 1 = straight trend, ~0 = noise."""
    if i - period < 0:
        return None
    net = abs(closes[i] - closes[i - period])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(i - period + 1, i + 1))
    return net / path if path > 0 else 0.0

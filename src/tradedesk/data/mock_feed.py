"""Deterministic synthetic market data. ALWAYS labelled MOCK. Not real prices.

Used to exercise the pipeline (validation, agents, backtests, paper trading) while
no real data source is reachable. Results on mock data say nothing about real
markets and are flagged as such everywhere they are shown.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

from ..schemas.market import Candle, DataSource, Quote, Timeframe
from .instruments import mock_params

MOCK_SOURCE = DataSource(name="MOCK", is_mock=True)
ANCHOR_UTC = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)  # a Monday
M5 = timedelta(minutes=5)


def market_open(t: datetime) -> bool:
    """Approximate CFD metals/FX schedule in UTC: closed Fri 21:00 -> Sun 22:00 and daily 21:00-22:00."""
    wd, h = t.weekday(), t.hour
    if wd == 5:
        return False
    if wd == 4 and h >= 21:
        return False
    if wd == 6 and h < 22:
        return False
    if h == 21:
        return False
    return True


def _session_vol(t: datetime) -> float:
    h = t.hour
    if 7 <= h < 12:
        return 1.3   # London
    if 12 <= h < 17:
        return 1.6   # London/NY overlap
    if 17 <= h < 21:
        return 1.0
    return 0.6       # Asia


class MockFeed:
    def __init__(self, symbol: str = "XAUUSD", seed: int = 7, drift_scale: float = 1.0):
        self.symbol = symbol
        _, digits, point, _, start_price, daily_vol_pct, spread = mock_params(symbol)
        self.digits, self.point, self.spread = digits, point, spread
        self.start_price = start_price
        self.bar_sigma = daily_vol_pct / 100 / math.sqrt(276)  # ~276 open M5 bars per day
        h = int(hashlib.sha256(f"{symbol}:{seed}".encode()).hexdigest()[:8], 16)
        self._rng = random.Random(h)
        self._bars: list[Candle] = []
        self._t = ANCHOR_UTC
        self._price = start_price
        self._drift = 0.0
        self._regime_left = 0
        self.drift_scale = drift_scale

    def _step(self) -> Candle | None:
        t = self._t
        self._t = t + M5
        if not market_open(t):
            return None
        rng = self._rng
        if self._regime_left <= 0:
            self._regime_left = rng.randint(150, 1500)
            self._drift = rng.choice([-1, 0, 0, 1]) * self.bar_sigma * rng.uniform(0.01, 0.06) * self.drift_scale
        self._regime_left -= 1
        sig = self.bar_sigma * _session_vol(t)
        o = self._price
        path = [o]
        p = o
        for _ in range(4):
            p *= math.exp(self._drift / 4 + sig / 2 * rng.gauss(0, 1))
            path.append(p)
        c = path[-1]
        hi = max(path) * (1 + abs(rng.gauss(0, sig / 4)))
        lo = min(path) * (1 - abs(rng.gauss(0, sig / 4)))
        self._price = c
        r = lambda x: round(x, self.digits)  # noqa: E731
        spread = self.spread * (2.5 if t.hour in (20, 22) else 1.0) * rng.uniform(0.8, 1.3)
        return Candle(symbol=self.symbol, timeframe=Timeframe.M5, open_time_utc=t, open=r(o),
                      high=max(r(hi), r(o), r(c)), low=min(r(lo), r(o), r(c)), close=r(c),
                      tick_volume=rng.randint(50, 900), spread_points=round(spread, 1), is_closed=True)

    def _extend_to(self, end_utc: datetime) -> None:
        while self._t + M5 <= end_utc:
            c = self._step()
            if c is not None:
                self._bars.append(c)

    def candles_m5(self, start_utc: datetime, end_utc: datetime) -> list[Candle]:
        """Closed M5 candles with open_time in [start, end) and close <= end."""
        self._extend_to(end_utc)
        return [c for c in self._bars if start_utc <= c.open_time_utc and c.open_time_utc + M5 <= end_utc]

    def quote(self, now_utc: datetime) -> Quote | None:
        self._extend_to(now_utc)
        if not self._bars or not market_open(now_utc):
            return None
        last = self._bars[-1]
        half = (last.spread_points or self.spread) * self.point / 2
        return Quote(symbol=self.symbol, time_utc=last.open_time_utc + M5,
                     bid=round(last.close - half, self.digits), ask=round(last.close + half, self.digits),
                     source=MOCK_SOURCE)

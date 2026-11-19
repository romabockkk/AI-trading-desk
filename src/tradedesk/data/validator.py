"""Market data validation. Any ERROR-level issue makes the dataset INVALID => NO_TRADE."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..schemas.market import Candle, Quote, Timeframe
from .mock_feed import market_open


@dataclass(frozen=True)
class Issue:
    code: str
    level: str          # ERROR | WARN
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not any(i.level == "ERROR" for i in self.issues)

    def as_list(self) -> list[dict]:
        return [i.__dict__ for i in self.issues]


def validate_candles(candles: list[Candle], tf: Timeframe, now_utc: datetime, *,
                     max_age_seconds: float | None = None, max_gap_bars: int = 3,
                     min_bars: int = 50) -> ValidationResult:
    issues: list[Issue] = []
    if len(candles) < min_bars:
        issues.append(Issue("TOO_FEW_BARS", "ERROR", f"{len(candles)} < {min_bars}"))
    if not candles:
        return ValidationResult(tuple(issues))
    step = timedelta(seconds=tf.seconds)
    sym = {c.symbol for c in candles}
    if len(sym) != 1:
        issues.append(Issue("MIXED_SYMBOLS", "ERROR", str(sym)))
    seen = set()
    for i, c in enumerate(candles):
        if c.timeframe is not tf:
            issues.append(Issue("WRONG_TIMEFRAME", "ERROR", f"bar {i} is {c.timeframe.value}"))
            break
        if c.open_time_utc in seen:
            issues.append(Issue("DUPLICATE_BAR", "ERROR", c.open_time_utc.isoformat()))
        seen.add(c.open_time_utc)
        if int(c.open_time_utc.timestamp()) % tf.seconds != 0:
            issues.append(Issue("MISALIGNED_BAR", "ERROR", c.open_time_utc.isoformat()))
        if not c.is_closed or c.open_time_utc + step > now_utc:
            issues.append(Issue("UNCLOSED_BAR", "ERROR", c.open_time_utc.isoformat()))
        if i:
            prev = candles[i - 1]
            if c.open_time_utc <= prev.open_time_utc:
                issues.append(Issue("NON_MONOTONIC", "ERROR", c.open_time_utc.isoformat()))
            gap = c.open_time_utc - prev.open_time_utc
            if gap > step:
                # count missing bars that fall inside scheduled trading hours
                missing = 0
                t = prev.open_time_utc + step
                while t < c.open_time_utc and missing <= 10_000:
                    if market_open(t):
                        missing += 1
                    t += step
                if missing > max_gap_bars:
                    issues.append(Issue("GAP", "ERROR" if missing * tf.seconds >= 7200 else "WARN",
                                        f"{missing} missing bars {prev.open_time_utc.isoformat()} -> "
                                        f"{c.open_time_utc.isoformat()}"))
            if prev.close > 0 and abs(c.open / prev.close - 1) > 0.05:
                issues.append(Issue("PRICE_JUMP", "WARN", f"{prev.close} -> {c.open} at {c.open_time_utc.isoformat()}"))
    if max_age_seconds is not None:
        age = (now_utc - (candles[-1].open_time_utc + step)).total_seconds()
        if age > max_age_seconds and market_open(now_utc):
            issues.append(Issue("STALE_DATA", "ERROR", f"last bar closed {age:.0f}s ago > {max_age_seconds}s"))
    return ValidationResult(tuple(issues))


def validate_quote(q: Quote | None, now_utc: datetime, max_age_seconds: float, point: float,
                   max_spread_points: float) -> ValidationResult:
    if q is None:
        return ValidationResult((Issue("NO_QUOTE", "ERROR", "quote unavailable"),))
    issues = []
    age = (now_utc - q.time_utc).total_seconds()
    if age > max_age_seconds:
        issues.append(Issue("STALE_QUOTE", "ERROR", f"{age:.1f}s > {max_age_seconds}s"))
    if age < -5:
        issues.append(Issue("QUOTE_FROM_FUTURE", "ERROR", f"{age:.1f}s"))
    sp = q.spread_points(point)
    if sp > max_spread_points:
        issues.append(Issue("SPREAD_TOO_WIDE", "ERROR", f"{sp} > {max_spread_points} points"))
    return ValidationResult(tuple(issues))

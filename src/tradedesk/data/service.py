"""Market data service: fetch -> validate -> persist snapshot.

Sources
- MOCK (default here): deterministic synthetic data, flagged is_mock everywhere.
- BROKER: a BrokerAdapter (MT5) once available. Not used while BROKER_DEMO is BLOCKED.
CSV files are imported for research/backtests via data.csv_import (see /api/datasets).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..analysis.indicators import atr
from ..db import database as db
from ..schemas.market import Candle, Quote, SymbolSpec, Timeframe
from .instruments import PRIORITY, mock_spec
from .mock_feed import MockFeed, market_open
from .resample import resample
from .validator import validate_candles, validate_quote

ANALYSIS_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


@dataclass
class MarketSnapshot:
    snapshot_id: str
    symbol: str
    now_utc: datetime
    source: str
    is_mock: bool
    m5: list[Candle]
    bars: dict[Timeframe, list[Candle]]
    quote: Quote | None
    spec: SymbolSpec | None
    market_open: bool
    issues: list[dict] = field(default_factory=list)
    valid: bool = False

    def atr(self, tf: Timeframe = Timeframe.M15) -> float | None:
        b = self.bars.get(tf) or []
        a = atr(b, 14) if len(b) >= 15 else []
        return a[-1] if a else None


class MarketDataService:
    def __init__(self, source: str = "MOCK", history_days: int = 60, seed: int = 7):
        if source != "MOCK":
            raise ValueError(f"data source {source!r} not available in this environment (BROKER_DEMO BLOCKED)")
        self.source = source
        self.history_days = history_days
        self._feeds: dict[str, MockFeed] = {}
        self.seed = seed

    def symbols(self) -> list[str]:
        return list(PRIORITY)

    def feed(self, symbol: str) -> MockFeed:
        if symbol not in self._feeds:
            self._feeds[symbol] = MockFeed(symbol, seed=self.seed)
        return self._feeds[symbol]

    def spec(self, symbol: str) -> SymbolSpec:
        return mock_spec(symbol)

    def m5(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        return self.feed(symbol).candles_m5(start, end)

    def snapshot(self, symbol: str, now_utc: datetime, limits=None) -> MarketSnapshot:
        start = now_utc - timedelta(days=self.history_days)
        m5 = self.m5(symbol, start, now_utc)
        bars = {tf: resample(m5, tf, now_utc) for tf in ANALYSIS_TFS}
        spec = self.spec(symbol)
        q = self.feed(symbol).quote(now_utc)
        is_open = market_open(now_utc)
        issues: list[dict] = []
        max_age = limits.max_candle_age_seconds_m5 if limits else 900
        v = validate_candles(m5, Timeframe.M5, now_utc, max_age_seconds=max_age, min_bars=500)
        issues += [dict(x, timeframe="M5") for x in v.as_list()]
        for tf in (Timeframe.H1, Timeframe.H4):
            vt = validate_candles(bars[tf], tf, now_utc, min_bars=50)
            issues += [dict(x, timeframe=tf.value) for x in vt.as_list()]
        if is_open:
            vq = validate_quote(q, now_utc, limits.max_quote_age_seconds if limits else 10, spec.point,
                                limits.max_spread_points if limits else 50)
            issues += [dict(x, timeframe="QUOTE") for x in vq.as_list()]
        else:
            issues.append({"code": "MARKET_CLOSED", "level": "WARN", "detail": "scheduled closure", "timeframe": "-"})
        valid = not any(i["level"] == "ERROR" for i in issues)
        return MarketSnapshot(f"snap-{uuid.uuid4().hex[:12]}", symbol, now_utc, self.source, True, m5, bars, q, spec,
                              is_open, issues, valid)


def persist_snapshot(conn: sqlite3.Connection, s: MarketSnapshot) -> str:
    last = s.m5[-1] if s.m5 else None
    digest = hashlib.sha256("|".join(f"{c.open_time_utc.isoformat()},{c.open},{c.high},{c.low},{c.close}"
                                     for c in s.m5).encode()).hexdigest()
    payload = {
        "bars": {tf.value: len(b) for tf, b in s.bars.items()},
        "last_closes": {tf.value: [c.close for c in b[-5:]] for tf, b in s.bars.items()},
        "last_bar_utc": {tf.value: (b[-1].open_time_utc.isoformat() if b else None) for tf, b in s.bars.items()},
        "spec_source": s.spec.source.name if s.spec else None,
        "market_open": s.market_open,
    }
    conn.execute(
        "INSERT INTO market_snapshots(snapshot_id, created_utc, source, is_mock, symbol, timeframe, data_from_utc,"
        " data_to_utc, bid, ask, spread_points, validation_status, validation_errors, payload, content_sha256)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s.snapshot_id, s.now_utc.isoformat(), s.source, int(s.is_mock), s.symbol, "M5",
         s.m5[0].open_time_utc.isoformat() if s.m5 else None, last.open_time_utc.isoformat() if last else None,
         s.quote.bid if s.quote else None, s.quote.ask if s.quote else None,
         s.quote.spread_points(s.spec.point) if (s.quote and s.spec) else None,
         "VALID" if s.valid else "INVALID", json.dumps(s.issues), json.dumps(payload), digest))
    if not s.valid:
        db.log_error(conn, "data_validator", "WARN", f"snapshot {s.snapshot_id} invalid",
                     details={"issues": [i for i in s.issues if i["level"] == "ERROR"][:20]})
    return s.snapshot_id

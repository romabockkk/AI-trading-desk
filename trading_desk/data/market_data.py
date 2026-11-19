"""Market data loading and validation (DATA_SOURCES.md: every decision traceable to its source)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .okx_client import OKXClient

BAR_MINUTES = {"15m": 15, "1H": 60, "4H": 240}
SOURCE = "OKX v5 REST /api/v5/market/candles"


def candles_to_frame(rows: list[list[str]], only_confirmed: bool = True) -> pd.DataFrame:
    """OKX rows [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm] (newest first) -> ascending DataFrame."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volQuote", "confirm"])
    if only_confirmed:
        df = df[df["confirm"] == "1"]
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    out = df.set_index("ts")[["open", "high", "low", "close", "volQuote"]].astype(float)
    out = out.rename(columns={"volQuote": "volume"})
    return out.sort_index()[~out.sort_index().index.duplicated()]


def resample(m15: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Build higher timeframes from M15 (keeps backtests and live on identical data)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = m15.resample(rule, label="left", closed="left").agg(agg).dropna()
    # drop the last bucket if it is not complete
    minutes = {"1h": 60, "4h": 240}[rule]
    if len(out) and len(m15):
        expected_last_m15 = out.index[-1] + pd.Timedelta(minutes=minutes - 15)
        if m15.index[-1] < expected_last_m15:
            out = out.iloc[:-1]
    return out


@dataclass
class MarketSnapshot:
    inst_id: str
    m15: pd.DataFrame
    h1: pd.DataFrame
    h4: pd.DataFrame
    instrument: dict = field(default_factory=dict)
    funding: dict | None = None
    open_interest: dict | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    problems: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def last_close_time(self) -> datetime | None:
        if self.m15.empty:
            return None
        return (self.m15.index[-1] + pd.Timedelta(minutes=15)).to_pydatetime()

    @property
    def price(self) -> float:
        return float(self.m15["close"].iloc[-1])


def validate(df: pd.DataFrame, bar: str, min_bars: int) -> list[str]:
    problems = []
    if len(df) < min_bars:
        problems.append(f"{bar}: only {len(df)} bars (need {min_bars})")
        return problems
    step = pd.Timedelta(minutes=BAR_MINUTES[bar])
    gaps = (df.index.to_series().diff().dropna() != step).sum()
    if gaps:
        problems.append(f"{bar}: {gaps} gap(s) in candle sequence")
    if (df["high"] < df[["open", "close"]].max(axis=1)).any() or (df["low"] > df[["open", "close"]].min(axis=1)).any():
        problems.append(f"{bar}: inconsistent OHLC values")
    return problems


def load_snapshot(client: OKXClient, inst_id: str, max_age_minutes: int = 20) -> MarketSnapshot:
    m15 = candles_to_frame(client.candles(inst_id, "15m", 300))
    h1 = candles_to_frame(client.candles(inst_id, "1H", 300))
    h4 = candles_to_frame(client.candles(inst_id, "4H", 300))
    snap = MarketSnapshot(inst_id, m15, h1, h4, sources=[f"{SOURCE} ({inst_id} 15m/1H/4H)"])
    snap.problems += validate(m15, "15m", 150) + validate(h1, "1H", 150) + validate(h4, "4H", 60)
    try:
        snap.instrument = client.instrument(inst_id)
        snap.sources.append("OKX /api/v5/public/instruments")
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        snap.problems.append(f"instrument spec unavailable: {exc}")
    for attr, fn, src in (("funding", client.funding_rate, "/public/funding-rate"),
                          ("open_interest", client.open_interest, "/public/open-interest")):
        try:
            setattr(snap, attr, fn(inst_id))
            snap.sources.append(f"OKX /api/v5{src}")
        except Exception as exc:  # noqa: BLE001
            snap.problems.append(f"{attr} unavailable: {exc}")
    if snap.last_close_time:
        age = (snap.fetched_at - snap.last_close_time).total_seconds() / 60
        if age > max_age_minutes:
            snap.problems.append(f"stale data: last closed M15 candle is {age:.0f} min old")
    return snap


def fetch_history_m15(client: OKXClient, inst_id: str, days: int, progress: bool = False) -> pd.DataFrame:
    """Download ``days`` of confirmed M15 candles via paginated history endpoint."""
    target = int(days * 24 * 4)
    rows: list[list[str]] = []
    after = None
    while len(rows) < target:
        batch = client.history_candles(inst_id, "15m", after=after, limit=100)
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]
        if progress and len(rows) % 2000 < 100:
            print(f"  {inst_id}: {len(rows)}/{target} candles")
    return candles_to_frame(rows)

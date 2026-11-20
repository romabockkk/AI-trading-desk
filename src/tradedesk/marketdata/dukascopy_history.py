"""Historical M1 candles from the Dukascopy Bank public datafeed (daily BID/ASK candle files).

URL: https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM0}/{DD}/{BID|ASK}_candles_min_1.bi5
     MM0 is the zero-based month (00 = January).
Record (24 bytes, big-endian): seconds-from-midnight-UTC (int32), open, close, low, high (int32 / divisor), volume (float32).
The O/C/L/H order was checked on real files (low <= open,close <= high for every record) and is re-checked on
every import; a file that fails the check is rejected. Minutes with zero volume (no trading, weekends) are dropped.

This is HISTORY for research/backtests. It is never presented as a live quote.
"""
from __future__ import annotations

import lzma
import sqlite3
import struct
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from ..db import database as db
from ..schemas.market import Candle, Timeframe
from .providers import DUKA_DIVISOR

UTC = timezone.utc
BASE = "https://datafeed.dukascopy.com/datafeed"
PROVIDER = "DUKASCOPY"


class HistoryError(RuntimeError):
    pass


def decode_candles(raw: bytes, day: date, divisor: int) -> list[tuple[datetime, float, float, float, float, float]]:
    if not raw:
        return []
    data = lzma.decompress(raw)
    if len(data) % 24:
        raise HistoryError(f"unexpected candle file length {len(data)}")
    out = []
    d0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    for off in range(0, len(data), 24):
        sec, o, c, lo, hi, vol = struct.unpack(">5if", data[off:off + 24])
        o, c, lo, hi = o / divisor, c / divisor, lo / divisor, hi / divisor
        if not (lo <= min(o, c) and max(o, c) <= hi):
            raise HistoryError(f"field order check failed at {d0 + timedelta(seconds=sec)}")
        out.append((d0 + timedelta(seconds=sec), o, hi, lo, c, vol))
    return out


def merge_bid_ask(symbol: str, bid: list, ask: list) -> list[Candle]:
    """M1 candles on BID prices (MT5 convention) with spread_price = ask close - bid close."""
    ask_close = {t: c for t, _o, _h, _l, c, _v in ask}
    out = []
    for t, o, h, lo, c, v in bid:
        if v <= 0:
            continue                       # no trading in this minute
        ac = ask_close.get(t)
        out.append(Candle(symbol=symbol, timeframe=Timeframe.M1, open_time_utc=t, open=o, high=h, low=lo, close=c,
                          tick_volume=v, is_closed=True,
                          spread_price=round(ac - c, 10) if ac is not None and ac >= c else None))
    return out


class DukascopyHistory:
    def __init__(self, client: httpx.Client | None = None, pause_s: float = 4.0, max_retries: int = 3):
        self.client = client or httpx.Client(timeout=30.0, headers={"User-Agent": "tradedesk-paper-research/1.0"})
        self.pause_s = pause_s
        self.max_retries = max_retries

    def _get(self, url: str) -> bytes:
        delay = self.pause_s
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.get(url)
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code == 200:
                return r.content
            if r is not None and r.status_code == 404:
                return b""
            if attempt == self.max_retries:
                code = r.status_code if r is not None else "network error"
                raise HistoryError(f"{url}: {code}")
            delay = max(delay * 3, 30.0) if (r is not None and r.status_code in (429, 503)) else delay * 2
            time.sleep(delay)
        return b""

    def day(self, symbol: str, d: date) -> list[Candle]:
        if symbol not in DUKA_DIVISOR:
            raise HistoryError(f"no Dukascopy divisor for {symbol}")
        div = DUKA_DIVISOR[symbol]
        path = f"{BASE}/{symbol}/{d.year}/{d.month - 1:02d}/{d.day:02d}"
        bid = decode_candles(self._get(f"{path}/BID_candles_min_1.bi5"), d, div)
        time.sleep(self.pause_s)
        ask = decode_candles(self._get(f"{path}/ASK_candles_min_1.bi5"), d, div)
        time.sleep(self.pause_s)
        return merge_bid_ask(symbol, bid, ask)


def stored_days(conn: sqlite3.Connection, symbol: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT substr(open_time_utc,1,10) FROM bars WHERE symbol=? AND"
                                       " timeframe='M1' AND provider=?", (symbol, PROVIDER))}


def download(conn: sqlite3.Connection, symbol: str, start: date, end: date, fetcher: DukascopyHistory | None = None,
             progress=None) -> dict:
    """Download [start, end] (UTC dates, only completed days), resumable. Stores M1 and derived M5 bars."""
    from ..data.resample import resample
    from ..data.service import store_bars
    fetcher = fetcher or DukascopyHistory()
    have = stored_days(conn, symbol)
    today = datetime.now(UTC).date()
    done, skipped, failed, bars = 0, 0, [], 0
    d = start
    while d <= end:
        if d >= today or d.weekday() == 5 or d.isoformat() in have:      # Saturdays have no trading
            skipped += 1
            d += timedelta(days=1)
            continue
        try:
            m1 = fetcher.day(symbol, d)
        except HistoryError as e:
            failed.append({"day": d.isoformat(), "error": str(e)})
            db.log_error(conn, "history:dukascopy", "WARN", str(e))
            d += timedelta(days=1)
            continue
        if m1:
            as_of = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
            bars += store_bars(conn, m1, PROVIDER)
            bars += store_bars(conn, resample(m1, Timeframe.M5, as_of), PROVIDER)
        done += 1
        if progress:
            progress(d, len(m1))
        d += timedelta(days=1)
    db.audit(conn, "SYSTEM", "HISTORY_DOWNLOADED", details={"provider": PROVIDER, "symbol": symbol,
                                                            "start": start.isoformat(), "end": end.isoformat(),
                                                            "days": done, "failed": len(failed)})
    return {"symbol": symbol, "days_downloaded": done, "days_skipped": skipped, "failed": failed, "bars_stored": bars}

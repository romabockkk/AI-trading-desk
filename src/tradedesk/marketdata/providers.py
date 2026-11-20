"""Read-only market data providers. None of them can place orders.

OANDA  — official documented REST v20 API (practice environment). Real-time bid/ask and BA candles.
         Needs an API token (free practice account). Prices are OANDA's CFD prices, NOT Bybit's.
DUKASCOPY — public historical datafeed of Dukascopy Bank (tick bid/ask, LZMA ".bi5" files).
         No key. Data for the current hour is typically not published until later, so live latency can be
         an hour or more; quotes older than the risk limit are treated as stale => NO TRADE.
         The binary layout follows widely used community documentation; it is not an official Dukascopy spec.
MOCK   — synthetic data for tests/development only. Used only when named explicitly.
"""
from __future__ import annotations

import lzma
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..data.mock_feed import MockFeed
from ..data.resample import resample
from ..schemas.market import Candle, DataSource, Quote, Timeframe

UTC = timezone.utc


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderQuote:
    quote: Quote
    tradeable: bool | None      # provider's own "tradeable" flag when it has one


def _parse_rfc3339(s: str) -> datetime:
    # OANDA returns nanoseconds: 2026-11-19T23:59:58.123456789Z
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


class OandaProvider:
    name = "OANDA"
    GRAN = {Timeframe.M1: "M1", Timeframe.M5: "M5", Timeframe.M15: "M15", Timeframe.H1: "H1", Timeframe.H4: "H4",
            Timeframe.D1: "D"}

    def __init__(self, base_url: str, token: str, account_id: str, symbol_map: dict[str, str],
                 client: httpx.Client | None = None, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        # When the token is stored as a network secret of the hosting environment, the egress proxy
        # injects it; the placeholder below is then replaced and never sent anywhere else.
        self.headers = {"Authorization": f"Bearer {token or 'injected-by-proxy'}", "Accept-Datetime-Format": "RFC3339"}
        self.account_id = account_id
        self.map = symbol_map
        self.client = client or httpx.Client(timeout=timeout)
        self.source = DataSource(name="OANDA v20 (practice, read-only)", is_mock=False)

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self.client.get(self.base + path, headers=self.headers, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(f"OANDA network error: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise ProviderError(f"OANDA auth failed ({r.status_code}): token missing or invalid")
        if r.status_code != 200:
            raise ProviderError(f"OANDA HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _account(self) -> str:
        if not self.account_id:
            accts = self._get("/v3/accounts").get("accounts", [])
            if not accts:
                raise ProviderError("OANDA: no accounts for this token")
            self.account_id = accts[0]["id"]
        return self.account_id

    def supports(self, symbol: str) -> bool:
        return symbol in self.map

    def quotes(self, symbols: list[str]) -> dict[str, ProviderQuote]:
        inst = {self.map[s]: s for s in symbols if s in self.map}
        if not inst:
            return {}
        data = self._get(f"/v3/accounts/{self._account()}/pricing", {"instruments": ",".join(inst)})
        out = {}
        for p in data.get("prices", []):
            sym = inst.get(p.get("instrument"))
            if not sym or not p.get("bids") or not p.get("asks"):
                continue
            q = Quote(symbol=sym, time_utc=_parse_rfc3339(p["time"]), bid=float(p["bids"][0]["price"]),
                      ask=float(p["asks"][0]["price"]), source=self.source)
            out[sym] = ProviderQuote(q, bool(p.get("tradeable")) if "tradeable" in p else None)
        return out

    def candles(self, symbol: str, tf: Timeframe, count: int = 500) -> list[Candle]:
        if symbol not in self.map:
            raise ProviderError(f"OANDA: no mapping for {symbol}")
        data = self._get(f"/v3/instruments/{self.map[symbol]}/candles",
                         {"granularity": self.GRAN[tf], "count": min(count, 5000), "price": "BA"})
        out = []
        for c in data.get("candles", []):
            if "bid" not in c or "ask" not in c:
                continue
            b, a = c["bid"], c["ask"]
            spread = float(a["c"]) - float(b["c"])
            out.append(Candle(symbol=symbol, timeframe=tf, open_time_utc=_parse_rfc3339(c["time"]), open=float(b["o"]),
                              high=float(b["h"]), low=float(b["l"]), close=float(b["c"]),
                              tick_volume=float(c.get("volume", 0)), is_closed=bool(c.get("complete")),
                              spread_price=round(max(spread, 0.0), 10)))
        return out


# Dukascopy integer price divisors (community-documented)
DUKA_DIVISOR = {"XAUUSD": 1000, "XAGUSD": 1000, "EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 1000}


def decode_bi5_ticks(raw: bytes, hour_start: datetime, divisor: int) -> list[tuple[datetime, float, float]]:
    """Return [(time, bid, ask)] from an hourly *_ticks.bi5 file (20-byte records: ms, ask, bid, askvol, bidvol)."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    out = []
    for off in range(0, len(data) - len(data) % 20, 20):
        ms, ask, bid, _av, _bv = struct.unpack(">3I2f", data[off:off + 20])
        out.append((hour_start + timedelta(milliseconds=ms), bid / divisor, ask / divisor))
    return out


class DukascopyProvider:
    name = "DUKASCOPY"
    BASE = "https://datafeed.dukascopy.com/datafeed"

    def __init__(self, symbol_map: dict[str, str], client: httpx.Client | None = None, min_interval_s: float = 1.0,
                 lookback_hours: int = 6):
        self.map = symbol_map
        self.client = client or httpx.Client(timeout=20.0, headers={"User-Agent": "tradedesk-paper-research/1.0"})
        self.min_interval = min_interval_s
        self.lookback_hours = lookback_hours
        self._last_call = 0.0
        self._backoff_until = 0.0
        self.source = DataSource(name="Dukascopy datafeed (public tick history)", is_mock=False)

    def supports(self, symbol: str) -> bool:
        return symbol in self.map and self.map[symbol] in DUKA_DIVISOR

    def _fetch(self, url: str) -> bytes:
        now = time.monotonic()
        if now < self._backoff_until:
            raise ProviderError(f"Dukascopy rate-limited; backing off {self._backoff_until - now:.0f}s")
        wait = self.min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        try:
            r = self.client.get(url)
        except httpx.HTTPError as e:
            raise ProviderError(f"Dukascopy network error: {type(e).__name__}") from e
        if r.status_code in (429, 503):
            self._backoff_until = time.monotonic() + 300
            raise ProviderError(f"Dukascopy throttled (HTTP {r.status_code}); pausing 5 min")
        if r.status_code == 404:
            return b""
        if r.status_code != 200:
            raise ProviderError(f"Dukascopy HTTP {r.status_code}")
        return r.content

    def ticks_for_hour(self, symbol: str, hour: datetime) -> list[tuple[datetime, float, float]]:
        d = self.map[symbol]
        url = f"{self.BASE}/{d}/{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        return decode_bi5_ticks(self._fetch(url), hour, DUKA_DIVISOR[d])

    def quotes(self, symbols: list[str], now: datetime | None = None) -> dict[str, ProviderQuote]:
        now = now or datetime.now(UTC)
        out = {}
        for s in symbols:
            if not self.supports(s):
                continue
            hour = now.replace(minute=0, second=0, microsecond=0)
            for k in range(self.lookback_hours):
                ticks = self.ticks_for_hour(s, hour - timedelta(hours=k))
                if ticks:
                    t, bid, ask = ticks[-1]
                    out[s] = ProviderQuote(Quote(symbol=s, time_utc=t, bid=bid, ask=ask, source=self.source), None)
                    break
        return out

    def candles(self, symbol: str, tf: Timeframe, count: int = 500, now: datetime | None = None) -> list[Candle]:
        """M1 bars built from ticks of the last `lookback_hours`; resampled for higher TFs (limited depth)."""
        now = now or datetime.now(UTC)
        hour = now.replace(minute=0, second=0, microsecond=0)
        ticks = []
        for k in reversed(range(self.lookback_hours)):
            ticks += self.ticks_for_hour(symbol, hour - timedelta(hours=k))
        m1 = ticks_to_bars(symbol, ticks, Timeframe.M1, now)
        bars = m1 if tf is Timeframe.M1 else resample(m1, tf, now)
        return bars[-count:]


def ticks_to_bars(symbol: str, ticks: list[tuple[datetime, float, float]], tf: Timeframe, now: datetime) -> list[Candle]:
    out, cur = [], None
    for t, bid, ask in ticks:
        epoch = int(t.timestamp())
        b = datetime.fromtimestamp(epoch - epoch % tf.seconds, tz=UTC)
        if cur is None or cur["t"] != b:
            if cur:
                out.append(cur)
            cur = {"t": b, "o": bid, "h": bid, "l": bid, "c": bid, "v": 0, "sp": ask - bid}
        cur["h"], cur["l"], cur["c"] = max(cur["h"], bid), min(cur["l"], bid), bid
        cur["v"] += 1
        cur["sp"] = max(cur["sp"], ask - bid)
    if cur:
        out.append(cur)
    return [Candle(symbol=symbol, timeframe=tf, open_time_utc=d["t"], open=d["o"], high=d["h"], low=d["l"], close=d["c"],
                   tick_volume=d["v"], spread_price=round(max(d["sp"], 0.0), 10),
                   is_closed=d["t"] + timedelta(seconds=tf.seconds) <= now) for d in out]


class MockProvider:
    """Synthetic data. Every object is labelled is_mock=True; the Risk Gate refuses mock quotes."""
    name = "MOCK"

    def __init__(self, seed: int = 7):
        self.feeds: dict[str, MockFeed] = {}
        self.seed = seed
        self.source = DataSource(name="MOCK", is_mock=True)

    def feed(self, s: str) -> MockFeed:
        if s not in self.feeds:
            self.feeds[s] = MockFeed(s, seed=self.seed)
        return self.feeds[s]

    def supports(self, symbol: str) -> bool:
        from ..data.instruments import _MOCK
        return symbol in _MOCK

    def quotes(self, symbols: list[str], now: datetime | None = None) -> dict[str, ProviderQuote]:
        now = now or datetime.now(UTC)
        out = {}
        for s in symbols:
            if self.supports(s):
                q = self.feed(s).quote(now)
                if q:
                    out[s] = ProviderQuote(q, True)
        return out

    def candles(self, symbol: str, tf: Timeframe, count: int = 500, now: datetime | None = None) -> list[Candle]:
        now = now or datetime.now(UTC)
        m5 = self.feed(symbol).candles_m5(now - timedelta(seconds=tf.seconds * (count + 50)), now)
        bars = m5 if tf is Timeframe.M5 else resample(m5, tf, now)
        return bars[-count:]

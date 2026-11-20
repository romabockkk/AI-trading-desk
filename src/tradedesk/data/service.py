"""Market data service: providers -> validation -> SQLite (latest_quotes, bars, market_snapshots).

Provider chain comes from TRADEDESK_DATA_SOURCE (e.g. "OANDA,DUKASCOPY"). MOCK is only used when named.
Every snapshot records the provider, quote time, receive time and latency. No data => invalid => NO TRADE.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..analysis.indicators import atr
from ..db import database as db
from ..execution.paper import Instrument
from ..marketdata import specs as specstore
from ..marketdata.providers import DukascopyProvider, MockProvider, OandaProvider, ProviderError, ProviderQuote
from ..schemas.market import Candle, Quote, SymbolSpec, Timeframe
from .mock_feed import market_open as schedule_open
from .validator import validate_candles, validate_quote

UTC = timezone.utc
ANALYSIS_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
BAR_COUNT = {Timeframe.M5: 500, Timeframe.M15: 500, Timeframe.H1: 500, Timeframe.H4: 400}


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
    instrument: Instrument | None
    spec_missing: list[str]
    market_open: bool
    issues: list[dict] = field(default_factory=list)
    valid: bool = False
    quote_received_utc: datetime | None = None
    quote_latency_s: float | None = None

    @property
    def spec(self) -> SymbolSpec | None:
        return self.instrument.spec if self.instrument else None

    def atr(self, tf: Timeframe = Timeframe.M15) -> float | None:
        b = [x for x in (self.bars.get(tf) or []) if x.is_closed]
        a = atr(b, 14) if len(b) >= 15 else []
        return a[-1] if a else None


def build_providers(settings) -> list:
    out = []
    seed = specstore.load_seed()
    for name in settings.providers:
        if name == "OANDA":
            out.append(OandaProvider(settings.oanda_base_url, settings.oanda_api_token.get_secret_value(),
                                     settings.oanda_account_id,
                                     {s: d["provider_symbols"]["OANDA"] for s, d in seed.items()
                                      if "OANDA" in d.get("provider_symbols", {})}))
        elif name == "DUKASCOPY":
            out.append(DukascopyProvider({s: d["provider_symbols"]["DUKASCOPY"] for s, d in seed.items()
                                          if "DUKASCOPY" in d.get("provider_symbols", {})}))
        elif name == "MOCK":
            out.append(MockProvider())
        else:
            raise ValueError(f"unknown data provider {name}")
    return out


class MarketService:
    def __init__(self, providers: list, symbols: tuple[str, ...], max_quote_age_s: float = 10.0):
        self.providers = providers
        self.symbols = symbols
        self.max_quote_age_s = max_quote_age_s
        self._bar_cache: dict[tuple, tuple[datetime, list[Candle], str]] = {}
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        return ",".join(p.name for p in self.providers) or "NONE"

    # ----------------------------------------------------------------- quotes
    def _status(self, conn, name: str, ok: bool, err: str | None = None) -> None:
        now = db.utc_now_iso()
        if ok:
            conn.execute("INSERT INTO provider_status(provider, last_ok_utc, consecutive_errors) VALUES (?,?,0)"
                         " ON CONFLICT(provider) DO UPDATE SET last_ok_utc=excluded.last_ok_utc, consecutive_errors=0",
                         (name, now))
        else:
            conn.execute("INSERT INTO provider_status(provider, last_error_utc, last_error, consecutive_errors) VALUES (?,?,?,1)"
                         " ON CONFLICT(provider) DO UPDATE SET last_error_utc=excluded.last_error_utc,"
                         " last_error=excluded.last_error, consecutive_errors=provider_status.consecutive_errors+1",
                         (name, now, (err or "")[:300]))

    def poll_quotes(self, conn: sqlite3.Connection, now: datetime | None = None,
                    symbols: tuple[str, ...] | None = None) -> dict[str, ProviderQuote]:
        now = now or datetime.now(UTC)
        want = list(symbols or self.symbols)
        got: dict[str, ProviderQuote] = {}
        for p in self.providers:
            need = [s for s in want if s not in got and p.supports(s)]
            if not need:
                continue
            try:
                res = p.quotes(need, now) if p.name != "OANDA" else p.quotes(need)
                self._status(conn, p.name, True)
            except ProviderError as e:
                self._status(conn, p.name, False, str(e))
                n = conn.execute("SELECT consecutive_errors FROM provider_status WHERE provider=?", (p.name,)).fetchone()[0]
                if n in (1, 10, 100) or n % 1000 == 0:      # avoid flooding the error log while a provider is down
                    db.log_error(conn, f"provider:{p.name}", "WARN", f"{e} (consecutive failures: {n})")
                continue
            for s, pq in res.items():
                got[s] = pq
                conn.execute("INSERT INTO latest_quotes(symbol, provider, quote_time_utc, received_utc, bid, ask, tradeable)"
                             " VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET provider=excluded.provider,"
                             " quote_time_utc=excluded.quote_time_utc, received_utc=excluded.received_utc,"
                             " bid=excluded.bid, ask=excluded.ask, tradeable=excluded.tradeable",
                             (s, p.name, pq.quote.time_utc.isoformat(), now.isoformat(), pq.quote.bid, pq.quote.ask,
                              None if pq.tradeable is None else int(pq.tradeable)))
        return got

    def latest(self, conn: sqlite3.Connection, symbol: str) -> dict | None:
        r = conn.execute("SELECT * FROM latest_quotes WHERE symbol=?", (symbol,)).fetchone()
        if r is None:
            return None
        p = next((x for x in self.providers if x.name == r["provider"]), None)
        src = p.source if p else None
        from ..schemas.market import DataSource
        q = Quote(symbol=symbol, time_utc=datetime.fromisoformat(r["quote_time_utc"]), bid=r["bid"], ask=r["ask"],
                  source=src or DataSource(name=r["provider"], is_mock=r["provider"] == "MOCK"))
        return {"quote": q, "provider": r["provider"], "received_utc": datetime.fromisoformat(r["received_utc"]),
                "tradeable": None if r["tradeable"] is None else bool(r["tradeable"])}

    def quote_status(self, conn: sqlite3.Connection, now: datetime) -> list[dict]:
        out = []
        for s in self.symbols:
            lt = self.latest(conn, s)
            if lt is None:
                out.append({"symbol": s, "provider": None, "status": "NO DATA"})
                continue
            q = lt["quote"]
            age = (now - q.time_utc).total_seconds()
            out.append({"symbol": s, "provider": lt["provider"], "bid": q.bid, "ask": q.ask,
                        "quote_time_utc": q.time_utc.isoformat(), "received_utc": lt["received_utc"].isoformat(),
                        "age_s": round(age, 1), "delivery_delay_s": round((lt["received_utc"] - q.time_utc).total_seconds(), 1),
                        "status": "LIVE" if age <= self.max_quote_age_s else "STALE", "is_mock": q.source.is_mock})
        return out

    # ----------------------------------------------------------------- bars
    def bars(self, conn: sqlite3.Connection, symbol: str, tf: Timeframe, now: datetime, count: int | None = None
             ) -> tuple[list[Candle], str | None]:
        count = count or BAR_COUNT.get(tf, 500)
        key = (symbol, tf)
        with self._lock:
            hit = self._bar_cache.get(key)
        if hit and (now - hit[0]).total_seconds() < min(tf.seconds, 300):
            return hit[1], hit[2]
        for p in self.providers:
            if not p.supports(symbol):
                continue
            try:
                bars = p.candles(symbol, tf, count) if p.name == "OANDA" else p.candles(symbol, tf, count, now)
                self._status(conn, p.name, True)
            except ProviderError as e:
                self._status(conn, p.name, False, str(e))
                continue
            if not bars:
                continue
            conn.executemany("INSERT OR REPLACE INTO bars(symbol, timeframe, open_time_utc, provider, o, h, l, c, spread_points,"
                             " volume) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [(symbol, tf.value, b.open_time_utc.isoformat(), p.name, b.open, b.high, b.low, b.close,
                               b.spread_price, b.tick_volume) for b in bars if b.is_closed])
            with self._lock:
                self._bar_cache[key] = (now, bars, p.name)
            return bars, p.name
        return [], None

    # ----------------------------------------------------------------- instrument / snapshot
    def instrument(self, conn: sqlite3.Connection, symbol: str, now: datetime) -> tuple[Instrument | None, list[str]]:
        st = specstore.status(conn, symbol)
        if not st["complete"]:
            return None, st["missing"]
        d = st["data"]
        conv = 1.0
        if d["profit_currency"] != "USD":
            pair = f"USD{d['profit_currency']}"
            lt = self.latest(conn, pair)
            if lt is None or (now - lt["quote"].time_utc).total_seconds() > max(self.max_quote_age_s, 120):
                return None, [f"fresh {pair} quote for currency conversion"]
            conv = 1.0 / ((lt["quote"].bid + lt["quote"].ask) / 2)
        spec = specstore.symbol_spec(conn, symbol, conv)
        return Instrument(spec, float(d["margin_pct"]), d["swap_type"], d["swap_triple_day"], conv), []

    def snapshot(self, conn: sqlite3.Connection, symbol: str, now: datetime, limits=None) -> MarketSnapshot:
        max_q = limits.max_quote_age_seconds if limits else self.max_quote_age_s
        issues: list[dict] = []
        self.poll_quotes(conn, now, (symbol,) + (("USDJPY",) if symbol != "USDJPY" else ()))
        lt = self.latest(conn, symbol)
        q = lt["quote"] if lt else None
        bars, prov = {}, None
        for tf in ANALYSIS_TFS:
            b, pn = self.bars(conn, symbol, tf, now)
            bars[tf] = [x for x in b if x.is_closed]
            prov = prov or pn
            v = validate_candles(bars[tf], tf, now, min_bars=50)
            issues += [dict(x, timeframe=tf.value) for x in v.as_list()]
        inst, missing = self.instrument(conn, symbol, now)
        tradeable = lt["tradeable"] if lt else None
        fresh = q is not None and (now - q.time_utc).total_seconds() <= max_q
        is_open = fresh and tradeable is not False and schedule_open(now)
        if q is None:
            issues.append({"code": "NO_QUOTE", "level": "ERROR", "detail": "no provider returned a quote", "timeframe": "QUOTE"})
        else:
            vq = validate_quote(q, now, max_q, (inst.spec.point if inst else 1e-9), limits.max_spread_points if (limits and inst) else 1e18)
            issues += [dict(x, timeframe="QUOTE") for x in vq.as_list()]
        if not is_open and q is not None and fresh:
            issues.append({"code": "MARKET_CLOSED", "level": "WARN", "detail": "provider not tradeable or scheduled closure",
                           "timeframe": "-"})
        valid = not any(i["level"] == "ERROR" for i in issues)
        is_mock = bool(q and q.source.is_mock) or prov == "MOCK"
        return MarketSnapshot(f"snap-{uuid.uuid4().hex[:12]}", symbol, now, (lt["provider"] if lt else prov) or "NONE", is_mock,
                              bars.get(Timeframe.M5, []), bars, q, inst, missing, is_open, issues, valid,
                              lt["received_utc"] if lt else None,
                              round((lt["received_utc"] - q.time_utc).total_seconds(), 2) if lt else None)


def persist_snapshot(conn: sqlite3.Connection, s: MarketSnapshot) -> str:
    last = s.m5[-1] if s.m5 else None
    digest = hashlib.sha256("|".join(f"{c.open_time_utc.isoformat()},{c.open},{c.high},{c.low},{c.close}"
                                     for c in s.m5).encode()).hexdigest()
    payload = {
        "bars": {tf.value: len(b) for tf, b in s.bars.items()},
        "last_closes": {tf.value: [c.close for c in b[-5:]] for tf, b in s.bars.items()},
        "last_bar_utc": {tf.value: (b[-1].open_time_utc.isoformat() if b else None) for tf, b in s.bars.items()},
        "spec_complete": s.instrument is not None, "spec_missing": s.spec_missing,
        "market_open": s.market_open, "quote_time_utc": s.quote.time_utc.isoformat() if s.quote else None,
        "quote_received_utc": s.quote_received_utc.isoformat() if s.quote_received_utc else None,
        "quote_latency_s": s.quote_latency_s,
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
        db.log_error(conn, "data_validator", "WARN", f"snapshot {s.snapshot_id} for {s.symbol} invalid",
                     details={"issues": [i for i in s.issues if i["level"] == "ERROR"][:20]})
    return s.snapshot_id


# ----------------------------------------------------------------------------- research history
HISTORY_PRIORITY = ("CSV", "OANDA", "DUKASCOPY", "MOCK")


def store_bars(conn: sqlite3.Connection, bars: list[Candle], provider: str) -> int:
    rows = [(b.symbol, b.timeframe.value, b.open_time_utc.isoformat(), provider, b.open, b.high, b.low, b.close,
             b.spread_price, b.tick_volume) for b in bars if b.is_closed]
    conn.executemany("INSERT OR REPLACE INTO bars(symbol, timeframe, open_time_utc, provider, o, h, l, c, spread_points, volume)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def history(conn: sqlite3.Connection, symbol: str, tf: Timeframe, start: datetime, end: datetime,
            allow_mock: bool = False) -> tuple[list[Candle], str | None]:
    """Stored closed bars for research, from the best provider that has any. Never silently falls back to MOCK."""
    for prov in HISTORY_PRIORITY:
        if prov == "MOCK" and not allow_mock:
            continue
        rows = conn.execute("SELECT * FROM bars WHERE symbol=? AND timeframe=? AND provider=? AND open_time_utc>=? AND"
                            " open_time_utc<? ORDER BY open_time_utc", (symbol, tf.value, prov, start.isoformat(),
                                                                       end.isoformat())).fetchall()
        if rows:
            return [Candle(symbol=symbol, timeframe=tf, open_time_utc=datetime.fromisoformat(r["open_time_utc"]), open=r["o"],
                           high=r["h"], low=r["l"], close=r["c"], tick_volume=r["volume"] or 0,
                           spread_price=r["spread_points"], is_closed=True) for r in rows], prov
    return [], None


def research_spec(conn: sqlite3.Connection, symbol: str) -> SymbolSpec:
    """Spec for R-multiple research backtests. Uses the stored spec when complete; otherwise a clearly labelled
    research-only sizing spec (R-multiples, win rate and expectancy do not depend on it; $ figures do)."""
    full = specstore.symbol_spec(conn, symbol, 1.0)
    if full is not None:
        return full
    from ..data.instruments import mock_spec
    try:
        s = mock_spec(symbol)
    except KeyError:
        raise ValueError(f"no spec for {symbol}") from None
    return s.model_copy(update={"source": s.source.model_copy(update={"name": "RESEARCH-ONLY sizing spec (not Bybit)"}),
                                "commission_per_lot": 0.0})

"""Dukascopy history import (format checked on real files 2026-11-20; tests use encoded fixtures) and the rule
'no fresh quote -> no new paper trade'."""
import lzma
import struct
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from helpers import T0, FixtureProvider, complete_gold_spec, market, settings
from tradedesk.data.service import coverage, history
from tradedesk.marketdata.dukascopy_history import (DukascopyHistory, HistoryError, decode_candles, download,
                                                    merge_bid_ask)
from tradedesk.marketdata.providers import ProviderQuote
from tradedesk.schemas.market import Quote, Timeframe

UTC = timezone.utc


def candle_file(rows):
    """rows: (sec, open, close, low, high, vol) in price units for XAUUSD (divisor 1000)."""
    raw = b"".join(struct.pack(">5if", s, round(o * 1000), round(c * 1000), round(lo * 1000), round(h * 1000), v)
                   for s, o, c, lo, h, v in rows)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_decode_order_and_validation():
    d = date(2026, 11, 18)
    out = decode_candles(candle_file([(0, 3849.175, 3847.226, 3847.145, 3849.698, 0.06)]), d, 1000)
    t, o, h, lo, c, v = out[0]
    assert t == datetime(2026, 11, 18, tzinfo=UTC) and (o, h, lo, c) == (3849.175, 3849.698, 3847.145, 3847.226)
    with pytest.raises(HistoryError, match="order"):
        decode_candles(candle_file([(0, 3849.0, 3847.0, 3850.0, 3846.0, 1.0)]), d, 1000)   # low > high


def test_merge_drops_zero_volume_and_sets_spread():
    d = date(2026, 11, 18)
    bid = decode_candles(candle_file([(0, 10, 11, 9, 12, 1.0), (60, 11, 11, 11, 11, 0.0)]), d, 1000)
    ask = decode_candles(candle_file([(0, 10.3, 11.4, 9.3, 12.3, 1.0), (60, 11.3, 11.3, 11.3, 11.3, 0.0)]), d, 1000)
    m = merge_bid_ask("XAUUSD", bid, ask)
    assert len(m) == 1 and m[0].spread_price == pytest.approx(0.4) and not m[0].tick_volume == 0


def test_download_is_resumable_and_month_zero_based(conn):
    urls = []

    def h(req):
        urls.append(req.url.path)
        side_ask = "ASK" in req.url.path
        rows = [(i * 60, 4000 + i * 0.01 + (0.3 if side_ask else 0), 4000 + i * 0.01 + 0.005 + (0.3 if side_ask else 0),
                 3999.9 + i * 0.01 + (0.3 if side_ask else 0), 4000.1 + i * 0.01 + (0.3 if side_ask else 0), 1.0)
                for i in range(30)]
        return httpx.Response(200, content=candle_file(rows))
    f = DukascopyHistory(client=httpx.Client(transport=httpx.MockTransport(h)), pause_s=0)
    r = download(conn, "XAUUSD", date(2026, 6, 8), date(2026, 6, 9), f)      # Mon, Tue
    assert r["days_downloaded"] == 2 and not r["failed"]
    assert urls[0] == "/datafeed/XAUUSD/2026/05/08/BID_candles_min_1.bi5"
    n = len(urls)
    r2 = download(conn, "XAUUSD", date(2026, 6, 8), date(2026, 6, 9), f)
    assert r2["days_downloaded"] == 0 and len(urls) == n                        # nothing re-downloaded
    bars, prov = history(conn, "XAUUSD", Timeframe.M5, datetime(2026, 6, 8, tzinfo=UTC), datetime(2026, 6, 10, tzinfo=UTC))
    assert prov == "DUKASCOPY" and len(bars) == 12 and bars[0].spread_price == pytest.approx(0.3)
    cov = {(c["timeframe"], c["provider"]): c for c in coverage(conn)}
    assert cov[("M1", "DUKASCOPY")]["bars"] == 60


def test_download_records_failures_without_crashing(conn):
    f = DukascopyHistory(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))), pause_s=0,
                         max_retries=0)
    r = download(conn, "XAUUSD", date(2026, 6, 8), date(2026, 6, 8), f)
    assert r["days_downloaded"] == 0 and r["failed"] and "429" in r["failed"][0]["error"]
    assert conn.execute("SELECT COUNT(*) FROM errors WHERE component='history:dukascopy'").fetchone()[0] == 1


class DelayedProvider(FixtureProvider):
    """Real-looking but DELAYED quotes (like Dukascopy history published ~25 min later)."""
    name = "DELAYED"

    def quotes(self, symbols, now=None):
        out = {}
        for s, pq in super().quotes(symbols, now).items():
            q = pq.quote
            out[s] = ProviderQuote(q.model_copy(update={"time_utc": q.time_utc - timedelta(minutes=25)}), None)
        return out


def test_no_new_paper_trade_without_fresh_quote(conn, monkeypatch):
    from test_orchestrator import _good_evidence, _inject_signal
    from tradedesk.agents.orchestrator import Orchestrator
    from helpers import LIMITS
    from tradedesk.learning import registry
    _inject_signal(monkeypatch, "LONG", _good_evidence())
    complete_gold_spec(conn)
    o = Orchestrator(conn, settings(tradedesk_paper_initial_balance=10_000.0), LIMITS, market(DelayedProvider()))
    registry.activate(conn, "TREND_CONTINUATION", "1.0.0", "OWNER", "test")
    r = o.run_cycle("XAUUSD", T0 + timedelta(minutes=2))
    assert r["action"] == "NO_TRADE"
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    snap = conn.execute("SELECT validation_errors FROM market_snapshots").fetchone()[0]
    assert "STALE_QUOTE" in snap


def test_old_history_bars_are_not_fresh(conn):
    """Bars that end long ago fail the freshness check -> snapshot invalid -> no trade."""
    m = market()
    s = m.snapshot(conn, "XAUUSD", T0 + timedelta(minutes=2))
    assert s.valid
    later = T0 + timedelta(days=1)          # Thursday, market open; cache still returns old bars
    m2 = market()
    m2._bar_cache = m._bar_cache            # simulate stale cached history
    for k, (t, b, p) in list(m2._bar_cache.items()):
        m2._bar_cache[k] = (later, b, p)
    s2 = m2.snapshot(conn, "XAUUSD", later)
    assert not s2.valid and any(i["code"] == "STALE_DATA" for i in s2.issues)

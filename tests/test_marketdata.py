"""Providers are tested against payloads shaped like their documented formats (OANDA v20 JSON, Dukascopy bi5).
These tests prove parsing and handling logic, NOT a live connection."""
import json
import lzma
import struct
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from helpers import T0, FixtureProvider, complete_gold_spec, market
from tradedesk.data.service import MarketService, history, persist_snapshot, research_spec, store_bars
from tradedesk.marketdata import specs
from tradedesk.marketdata.providers import (DukascopyProvider, MockProvider, OandaProvider, ProviderError,
                                            decode_bi5_ticks, ticks_to_bars)
from tradedesk.schemas.market import Timeframe

UTC = timezone.utc
NOW = T0 + timedelta(minutes=2, seconds=3)


def oanda(handler):
    return OandaProvider("https://api-fxpractice.oanda.com", "tok", "", {"XAUUSD": "XAU_USD", "EURUSD": "EUR_USD"},
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_oanda_pricing_and_candles_parsing():
    seen = []

    def h(req):
        seen.append(req)
        if req.url.path == "/v3/accounts":
            return httpx.Response(200, json={"accounts": [{"id": "101-004-1-001"}]})
        if req.url.path.endswith("/pricing"):
            return httpx.Response(200, json={"prices": [{"instrument": "XAU_USD", "time": "2026-11-20T10:00:01.123456789Z",
                                                         "tradeable": True, "bids": [{"price": "4012.35"}],
                                                         "asks": [{"price": "4012.71"}]}]})
        return httpx.Response(200, json={"candles": [
            {"complete": True, "volume": 120, "time": "2026-11-20T09:55:00.000000000Z",
             "bid": {"o": "4010.0", "h": "4013.0", "l": "4009.5", "c": "4012.0"},
             "ask": {"o": "4010.3", "h": "4013.3", "l": "4009.8", "c": "4012.4"}},
            {"complete": False, "volume": 10, "time": "2026-11-20T10:00:00.000000000Z",
             "bid": {"o": "4012.0", "h": "4012.5", "l": "4011.9", "c": "4012.3"},
             "ask": {"o": "4012.3", "h": "4012.8", "l": "4012.2", "c": "4012.7"}}]})
    p = oanda(h)
    q = p.quotes(["XAUUSD", "EURUSD"])
    assert q["XAUUSD"].quote.bid == 4012.35 and q["XAUUSD"].quote.ask == 4012.71 and q["XAUUSD"].tradeable is True
    assert q["XAUUSD"].quote.time_utc == datetime(2026, 11, 20, 10, 0, 1, 123456, tzinfo=UTC)
    assert not q["XAUUSD"].quote.source.is_mock
    c = p.candles("XAUUSD", Timeframe.M5)
    assert c[0].is_closed and not c[1].is_closed and c[0].spread_price == pytest.approx(0.4)
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    assert seen[1].url.params["instruments"] == "XAU_USD,EUR_USD"
    assert all(r.method == "GET" for r in seen)          # read-only


def test_oanda_auth_error_is_provider_error():
    p = oanda(lambda req: httpx.Response(401, json={"errorMessage": "Insufficient authorization"}))
    with pytest.raises(ProviderError, match="auth"):
        p.quotes(["XAUUSD"])


def _bi5(records):
    raw = b"".join(struct.pack(">3I2f", ms, ask, bid, 1.0, 1.0) for ms, ask, bid in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_dukascopy_bi5_decoding_and_bars():
    hour = datetime(2026, 11, 19, 14, tzinfo=UTC)
    ticks = decode_bi5_ticks(_bi5([(1000, 4012710, 4012350), (61000, 4013000, 4012600)]), hour, 1000)
    assert ticks[0] == (hour + timedelta(seconds=1), 4012.35, 4012.71)
    bars = ticks_to_bars("XAUUSD", ticks, Timeframe.M1, hour + timedelta(minutes=5))
    assert len(bars) == 2 and bars[0].close == 4012.35 and bars[0].spread_price == pytest.approx(0.36)


def test_dukascopy_url_month_zero_based_and_backoff():
    urls = []

    def h(req):
        urls.append(str(req.url))
        return httpx.Response(429, json={"error": "Too Many Requests"})
    p = DukascopyProvider({"XAUUSD": "XAUUSD"}, client=httpx.Client(transport=httpx.MockTransport(h)), min_interval_s=0)
    with pytest.raises(ProviderError, match="throttled"):
        p.ticks_for_hour("XAUUSD", datetime(2026, 11, 19, 14, tzinfo=UTC))
    assert urls[0].endswith("/XAUUSD/2026/10/19/14h_ticks.bi5")
    with pytest.raises(ProviderError, match="backing off"):
        p.ticks_for_hour("XAUUSD", datetime(2026, 11, 19, 13, tzinfo=UTC))
    assert len(urls) == 1                    # no hammering while backing off


def test_dukascopy_quote_from_latest_available_hour():
    hour = datetime(2026, 11, 19, 13, tzinfo=UTC)

    def h(req):
        if req.url.path.endswith("/13h_ticks.bi5"):
            return httpx.Response(200, content=_bi5([(5000, 4012710, 4012350)]))
        return httpx.Response(200, content=b"")
    p = DukascopyProvider({"XAUUSD": "XAUUSD"}, client=httpx.Client(transport=httpx.MockTransport(h)), min_interval_s=0)
    q = p.quotes(["XAUUSD"], now=datetime(2026, 11, 19, 15, 30, tzinfo=UTC))["XAUUSD"].quote
    assert q.time_utc == hour + timedelta(seconds=5) and q.bid == 4012.35   # 2.5h old -> will be STALE, honestly


def test_service_records_quotes_latency_and_staleness(conn):
    m = market()
    m.poll_quotes(conn, NOW)
    st = {r["symbol"]: r for r in m.quote_status(conn, NOW)}
    assert st["XAUUSD"]["status"] == "LIVE" and st["XAUUSD"]["provider"] == "FIXTURE"
    later = NOW + timedelta(minutes=5)
    assert {r["symbol"]: r for r in m.quote_status(conn, later)}["XAUUSD"]["status"] == "STALE"


def test_provider_failure_logged_and_falls_through(conn):
    class Broken(MockProvider):
        name = "BROKEN"

        def quotes(self, symbols, now=None):
            raise ProviderError("down")
    m = MarketService([Broken(), FixtureProvider()], ("XAUUSD",))
    got = m.poll_quotes(conn, NOW)
    assert "XAUUSD" in got
    row = conn.execute("SELECT * FROM provider_status WHERE provider='BROKEN'").fetchone()
    assert row["consecutive_errors"] == 1 and "down" in row["last_error"]


def test_no_provider_means_invalid_snapshot(conn):
    m = MarketService([], ("XAUUSD",))
    s = m.snapshot(conn, "XAUUSD", NOW)
    assert not s.valid and s.quote is None and any(i["code"] == "NO_QUOTE" for i in s.issues)


def test_snapshot_requires_complete_spec(conn):
    m = market()
    specs.seed(conn)
    s = m.snapshot(conn, "XAUUSD", NOW)
    assert s.instrument is None and "volume_min" in s.spec_missing
    complete_gold_spec(conn)
    s2 = m.snapshot(conn, "XAUUSD", NOW)
    assert s2.instrument is not None and s2.valid and s2.market_open
    assert s2.instrument.spec.tick_value == pytest.approx(100 * 0.01)       # derived: contract 100 x tick 0.01
    persist_snapshot(conn, s2)
    row = conn.execute("SELECT payload FROM market_snapshots").fetchone()
    assert json.loads(row[0])["quote_latency_s"] is not None


def test_mock_snapshot_flagged(conn):
    s = market(MockProvider()).snapshot(conn, "XAUUSD", NOW)
    assert s.is_mock and s.quote.source.is_mock


def test_seeded_specs_contain_only_sourced_values(conn):
    specs.seed(conn)
    g = specs.status(conn, "XAUUSD")
    assert g["data"]["contract_size"] == 100 and g["data"]["commission_per_lot"] == 6.0 and g["data"]["margin_pct"] == 0.2
    assert not g["complete"] and set(g["missing"]) >= {"volume_min", "volume_step", "tick_size", "swap_long"}
    for s in ("EURUSD", "USDJPY", "US500"):
        assert "contract_size" in specs.status(conn, s)["missing"]          # not confirmed -> not invented


def test_spec_update_rules(conn):
    specs.seed(conn)
    with pytest.raises(specs.SpecError, match="OWNER"):
        specs.update(conn, "XAUUSD", {"volume_min": 0.01}, "app", "CHIEF")
    with pytest.raises(specs.SpecError, match="source"):
        specs.update(conn, "XAUUSD", {"volume_min": 0.01}, " ", "OWNER")
    with pytest.raises(specs.SpecError, match="positive"):
        specs.update(conn, "XAUUSD", {"volume_min": 0}, "app", "OWNER")
    with pytest.raises(specs.SpecError, match="unknown field"):
        specs.update(conn, "XAUUSD", {"leverage_boost": 9}, "app", "OWNER")
    r = complete_gold_spec(conn)
    assert r["complete"] and r["field_sources"]["volume_min"].startswith("TEST FIXTURE")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='SPEC_UPDATED'").fetchone()[0] == 1


def test_jpy_conversion_needs_live_usdjpy(conn):
    specs.seed(conn)
    specs.update(conn, "USDJPY", {"contract_size": 100000, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 50,
                                  "tick_size": 0.001, "digits": 3, "margin_pct": 0.2, "swap_long": 1, "swap_short": -2,
                                  "swap_triple_day": "WEDNESDAY"}, "TEST FIXTURE", "OWNER")
    m = market(symbols=("USDJPY",))
    inst, miss = m.instrument(conn, "USDJPY", NOW)
    assert inst is None and "USDJPY" in miss[0]
    m.poll_quotes(conn, NOW)
    inst, _ = m.instrument(conn, "USDJPY", NOW)
    q = m.latest(conn, "USDJPY")["quote"]
    assert inst.usd_per_profit_ccy == pytest.approx(1 / ((q.bid + q.ask) / 2))


def test_history_never_silently_uses_mock(conn):
    m5 = MockProvider().candles("XAUUSD", Timeframe.M5, 300, NOW)
    store_bars(conn, m5, "MOCK")
    bars, prov = history(conn, "XAUUSD", Timeframe.M5, NOW - timedelta(days=5), NOW)
    assert bars == [] and prov is None
    bars, prov = history(conn, "XAUUSD", Timeframe.M5, NOW - timedelta(days=5), NOW, allow_mock=True)
    assert prov == "MOCK" and len(bars) == len([b for b in m5 if b.is_closed])
    assert "RESEARCH-ONLY" in research_spec(conn, "XAUUSD").source.name

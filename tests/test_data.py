from datetime import datetime, timedelta, timezone

import pytest

from helpers import T0, candle
from tradedesk.data.csv_import import CsvImportError, import_mt5_csv
from tradedesk.data.instruments import mock_spec
from tradedesk.data.mock_feed import MockFeed, market_open
from tradedesk.data.resample import resample
from tradedesk.data.validator import validate_candles, validate_quote
from tradedesk.schemas.market import Timeframe

UTC = timezone.utc


def test_mock_is_labelled_and_deterministic():
    a = MockFeed("XAUUSD", seed=3).candles_m5(T0 - timedelta(days=2), T0)
    b = MockFeed("XAUUSD", seed=3).candles_m5(T0 - timedelta(days=2), T0)
    assert a == b and len(a) > 300
    assert mock_spec("XAUUSD").source.is_mock
    q = MockFeed("XAUUSD").quote(T0)
    assert q.source.is_mock and q.ask > q.bid


def test_mock_respects_schedule():
    m5 = MockFeed("XAUUSD").candles_m5(datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 8, 3, tzinfo=UTC))
    assert all(market_open(c.open_time_utc) for c in m5)
    assert not market_open(datetime(2026, 6, 6, 12, tzinfo=UTC))        # Saturday
    assert not market_open(datetime(2026, 6, 10, 21, 30, tzinfo=UTC))   # daily break
    assert MockFeed("XAUUSD").quote(datetime(2026, 6, 6, 12, tzinfo=UTC)) is None


def test_mock_xau_tick_value_matches_contract():
    s = mock_spec("XAUUSD")
    assert s.contract_size == 100 and s.tick_size == 0.01 and s.tick_value == pytest.approx(1.0)


def test_validator_accepts_clean_mock():
    m5 = MockFeed("XAUUSD").candles_m5(T0 - timedelta(days=3), T0)
    assert validate_candles(m5, Timeframe.M5, T0, max_age_seconds=900).ok


def _codes(res):
    return {i.code for i in res.issues}


def test_validator_detects_problems():
    base = [candle(i, 10, 11, 9, 10.5, Timeframe.M5) for i in range(60)]
    assert "DUPLICATE_BAR" in _codes(validate_candles(base + [base[-1]], Timeframe.M5, T0 + timedelta(days=1)))
    gap = base[:30] + [candle(i, 10, 11, 9, 10.5, Timeframe.M5) for i in range(60, 90)]
    assert "GAP" in _codes(validate_candles(gap, Timeframe.M5, T0 + timedelta(days=1)))
    assert "UNCLOSED_BAR" in _codes(validate_candles(base, Timeframe.M5, T0 + timedelta(minutes=5 * 59 + 1)))
    stale = validate_candles(base, Timeframe.M5, T0 + timedelta(hours=10), max_age_seconds=900)
    assert "STALE_DATA" in _codes(stale) and not stale.ok
    assert "TOO_FEW_BARS" in _codes(validate_candles(base[:5], Timeframe.M5, T0 + timedelta(days=1)))
    mis = [c.model_copy(update={"open_time_utc": c.open_time_utc + timedelta(minutes=1)}) for c in base]
    assert "MISALIGNED_BAR" in _codes(validate_candles(mis, Timeframe.M5, T0 + timedelta(days=1)))


def test_weekend_gap_is_not_an_error():
    m5 = MockFeed("XAUUSD").candles_m5(datetime(2026, 6, 4, tzinfo=UTC), datetime(2026, 6, 9, tzinfo=UTC))
    r = validate_candles(m5, Timeframe.M5, datetime(2026, 6, 9, tzinfo=UTC))
    assert "GAP" not in _codes(r)


def test_quote_validation():
    from helpers import quote
    assert validate_quote(quote(t=T0), T0, 10, 0.01, 50).ok
    assert "STALE_QUOTE" in _codes(validate_quote(quote(t=T0 - timedelta(seconds=30)), T0, 10, 0.01, 50))
    assert "SPREAD_TOO_WIDE" in _codes(validate_quote(quote(bid=2650, ask=2651), T0, 10, 0.01, 50))
    assert "NO_QUOTE" in _codes(validate_quote(None, T0, 10, 0.01, 50))


def test_resample_buckets_and_excludes_open_bar():
    m5 = MockFeed("XAUUSD").candles_m5(T0 - timedelta(hours=3), T0 + timedelta(minutes=30))
    as_of = T0 + timedelta(minutes=30)
    h1 = resample(m5, Timeframe.H1, as_of)
    assert all(b.is_closed and b.open_time_utc.minute == 0 for b in h1)
    assert h1[-1].open_time_utc == T0 - timedelta(hours=1)
    grp = [c for c in m5 if h1[-1].open_time_utc <= c.open_time_utc < T0]
    assert h1[-1].high == max(c.high for c in grp) and h1[-1].close == grp[-1].close
    incl = resample(m5, Timeframe.H1, as_of, include_open=True)
    assert incl[-1].is_closed is False


def test_csv_import_converts_server_time(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2026.06.10\t12:00:00\t2650.1\t2651.0\t2649.5\t2650.5\t120\t0\t22\n")
    c, src = import_mt5_csv(p, "XAUUSD+", Timeframe.H1, "UTC+3")
    assert c[0].open_time_utc == datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
    assert c[0].spread_points == 22 and not src.is_mock
    c2, _ = import_mt5_csv(p, "XAUUSD+", Timeframe.H1, "Europe/Athens")
    assert c2[0].open_time_utc == datetime(2026, 6, 10, 9, 0, tzinfo=UTC)   # EEST = UTC+3 in June


def test_csv_import_rejects_bad_rows(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n2026.06.10\t12:00:00\t10\t9\t8\t9\n")
    with pytest.raises(CsvImportError):
        import_mt5_csv(p, "X", Timeframe.H1, "UTC")



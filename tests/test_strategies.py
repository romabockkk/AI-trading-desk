import random
from datetime import datetime, timedelta, timezone

import pytest

from tradedesk.backtest.engine import run_backtest
from tradedesk.data.instruments import mock_spec
from tradedesk.data.mock_feed import MockFeed
from tradedesk.data.resample import resample
from tradedesk.strategies.library import ALL_STRATEGIES, build

UTC = timezone.utc
START, END = datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def m5():
    return MockFeed("XAUUSD", seed=11).candles_m5(START, END)


def test_six_strategies_with_formal_rules():
    assert len(ALL_STRATEGIES) == 6
    for sid, cls in ALL_STRATEGIES.items():
        d = cls().describe()
        assert len(d["rules"]) > 150 and "Stop" in d["rules"] or "stop" in d["rules"].lower()
        assert cls.param_grid, sid


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        build("TREND_CONTINUATION", {"nonsense": 1})


@pytest.mark.parametrize("sid", list(ALL_STRATEGIES))
def test_no_lookahead_signals_identical_on_truncated_data(m5, sid):
    """A signal at bar i must be identical whether or not bars after i exist."""
    st = build(sid)
    as_of = m5[-1].open_time_utc + timedelta(minutes=5)
    bars = resample(m5, st.exec_tf, as_of)
    htf = {tf: resample(m5, tf, as_of) for tf in st.htfs}
    full = st.make_context(bars, htf)
    full_sigs = {i: st.signal_at(full, i) for i in range(200, len(bars))}
    hits = [i for i, s in full_sigs.items() if s] or [len(bars) - 1]
    rng = random.Random(0)
    probe = sorted(set(rng.sample(hits, min(6, len(hits))) + rng.sample(range(200, len(bars)), 6)))
    for i in probe:
        cut_time = bars[i].open_time_utc + timedelta(seconds=st.exec_tf.seconds)
        m5_cut = [c for c in m5 if c.open_time_utc + timedelta(minutes=5) <= cut_time]
        b2 = resample(m5_cut, st.exec_tf, cut_time)
        h2 = {tf: resample(m5_cut, tf, cut_time) for tf in st.htfs}
        st2 = build(sid)
        ctx2 = st2.make_context(b2, h2)
        assert len(b2) == i + 1
        assert st2.signal_at(ctx2, i) == full_sigs[i], f"{sid} look-ahead at bar {i}"


@pytest.mark.parametrize("sid", list(ALL_STRATEGIES))
def test_signal_geometry(m5, sid):
    res = run_backtest(build(sid), m5, mock_spec("XAUUSD"))
    for t in res.trades:
        if t.side == "LONG":
            assert t.stop < t.entry < t.target
        else:
            assert t.target < t.entry < t.stop


def test_null_hypothesis_no_edge_on_driftless_random_walk():
    """On a driftless synthetic market no strategy should show a large positive edge (detects leakage)."""
    spec = mock_spec("XAUUSD")
    exps = []
    for seed in (21, 22):
        m5 = MockFeed("XAUUSD", seed=seed, drift_scale=0.0).candles_m5(START, END)
        for sid in ALL_STRATEGIES:
            m = run_backtest(build(sid), m5, spec).metrics()
            if m["n_trades"] >= 20:
                exps.append(m["expectancy_r"])
    assert exps and sum(exps) / len(exps) < 0.15

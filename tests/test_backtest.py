from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from helpers import candle, series
from tradedesk.backtest.engine import (BacktestConfig, CostModel, _rollovers, bootstrap_expectancy, compute_metrics,
                                       monte_carlo_drawdown, run_backtest, walk_forward)
from tradedesk.data.instruments import mock_spec
from tradedesk.data.mock_feed import MockFeed
from tradedesk.schemas.market import Timeframe
from tradedesk.strategies.base import Signal, Strategy
from tradedesk.strategies.library import ALL_STRATEGIES, build

UTC = timezone.utc
SPEC = mock_spec("XAUUSD")


class OneShot(Strategy):
    """Test strategy: LONG at bar 60 with fixed SL/TP distances."""
    id, exec_tf, htfs = "ONESHOT", Timeframe.M15, ()
    default_params = {"sl": 5.0, "tp": 10.0}

    def signal_at(self, ctx, i):
        if i != 60:
            return None
        c = ctx.bars[i].close
        return Signal(self.id, "t", i, "", "LONG", c, c - self.params["sl"], c + self.params["tp"], "", "")


def m15_bars(path):
    return [candle(i, o, h, l, c, Timeframe.M15, spread=0) for i, (o, h, l, c) in enumerate(path)]


def run_on(bars, strat, costs=CostModel(default_spread_points=0, slippage_points=0)):
    return run_backtest(strat, [], SPEC, BacktestConfig(costs=costs), prebuilt=(bars, {}))


def flat(n, p=2650.0):
    return [(p, p + 0.5, p - 0.5, p)] * n


def test_entry_next_open_and_take_profit():
    bars = m15_bars(flat(61) + [(2651, 2652, 2650.5, 2651.5), (2651.5, 2662, 2651, 2661)] + flat(10, 2661))
    r = run_on(bars, OneShot())
    t = r.trades[0]
    assert t.entry == 2651 and t.exit_reason == "TAKE_PROFIT"
    assert t.target == pytest.approx(2650 + 10)   # fixed TP from the signal bar (no rr_target)
    assert t.r == pytest.approx((2660 - 2651) / (2651 - 2645), rel=1e-3)   # R measured from the real fill


def test_stop_first_when_both_hit_in_same_bar():
    bars = m15_bars(flat(61) + [(2650, 2700, 2600, 2650)] + flat(10))
    t = run_on(bars, OneShot()).trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.r == pytest.approx(-1.0, abs=1e-3)


def test_gap_through_stop_fills_at_open():
    bars = m15_bars(flat(62) + [(2640, 2641, 2639, 2640)] + flat(10, 2640))
    t = run_on(bars, OneShot()).trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.exit == 2640 and t.r < -1.5


def test_costs_reduce_result():
    bars = m15_bars(flat(61) + [(2650, 2650.5, 2649.5, 2650), (2650, 2662, 2649.8, 2661)] + flat(10, 2661))
    free = run_on(bars, OneShot()).trades[0]
    costly = run_on(bars, OneShot(), CostModel(default_spread_points=30, slippage_points=10,
                                                commission_per_lot_side=3.0)).trades[0]
    assert costly.net < free.net and costly.commission > 0


def test_rr_target_reanchored_to_fill():
    s = Signal("x", "1", 0, "", "LONG", 100.0, 99.0, 102.0, "", "", (), 2.0)
    assert s.target_for_entry(100.5) == pytest.approx(100.5 + 2 * 1.5)


def test_swap_rollovers_wednesday_triple():
    t0 = datetime(2026, 6, 9, 20, tzinfo=UTC)          # Tue 20:00
    assert _rollovers(t0, t0 + timedelta(hours=2)) == 1
    assert _rollovers(t0, t0 + timedelta(days=1, hours=2)) == 1 + 3
    cfg = BacktestConfig(costs=CostModel(swap_long_per_lot_night=-5.0, default_spread_points=0, slippage_points=0),
                         max_hold_bars=300)
    bars = m15_bars(flat(300))
    r = run_backtest(OneShot(), [], SPEC, cfg, prebuilt=(bars, {}))
    assert r.trades[0].swap < 0


def test_sizing_uses_risk_pct():
    bars = m15_bars(flat(61) + [(2650, 2650.5, 2649.5, 2650)] + [(2650, 2650, 2640, 2641)] + flat(5, 2641))
    t = run_on(bars, OneShot()).trades[0]
    assert t.risk_money <= 50.0 + 1e-6 and t.lots == pytest.approx(0.1)


def test_metrics():
    m = compute_metrics([{"r": 2, "net": 100}, {"r": -1, "net": -50}, {"r": -1, "net": -50}], [1000, 1100, 1050, 1000])
    assert m["n_trades"] == 3 and m["win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["profit_factor"] == 1.0 and m["expectancy_r"] == 0.0
    assert m["max_drawdown_pct"] == pytest.approx(100 / 1100 * 100, rel=1e-3)
    assert m["max_consecutive_losses"] == 2 and "insufficient" in m["sample_warning"]


def test_bootstrap_and_mc_small_sample_skipped():
    assert bootstrap_expectancy([1, -1])["ci95"] is None
    assert "skipped" in monte_carlo_drawdown([1, -1], 0.5)["note"]
    b = bootstrap_expectancy([1.0] * 20 + [-1.0] * 10)
    assert b["ci95"][0] < b["mean_r"] < b["ci95"][1]


def test_walk_forward_out_of_sample_only():
    m5 = MockFeed("XAUUSD", seed=5).candles_m5(datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC))
    wf = walk_forward(ALL_STRATEGIES["TREND_CONTINUATION"], m5, SPEC, folds=3)
    assert len(wf["folds"]) == 3
    for f in wf["folds"]:
        assert f["test_from"] is not None
    starts = [f["test_from"] for f in wf["folds"]]
    assert starts == sorted(starts)
    for t in wf["oos_trades"]:
        assert t["entry_time"] >= wf["folds"][0]["test_from"]
    assert wf["oos_metrics"]["data_is_mock"] is True


def test_walk_forward_needs_data():
    m5 = MockFeed("XAUUSD").candles_m5(datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 9, tzinfo=UTC))
    assert "error" in walk_forward(ALL_STRATEGIES["MEAN_REVERSION"], m5, SPEC, folds=4)

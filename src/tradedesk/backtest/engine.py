"""Event-driven bar backtester with realistic costs.

Assumptions (all explicit, all configurable):
- Candles are BID prices (MT5 convention). Longs enter at ask = open + spread, exit at bid.
  Shorts enter at bid, exit at ask (stop/target compared against bid + spread).
- Signal at bar i (closed) -> entry at open of bar i+1 + `latency_bars` extra bars, plus slippage.
- If stop and target are both inside one bar, the STOP is assumed hit first (conservative).
- A gap through the stop fills at the bar open (worse than the stop).
- Swap is charged per position per rollover (21:00 UTC), x3 on Wednesdays.
- Position size from risk.sizing using the instrument spec and current equity.
- Daily drawdown limit stops new entries for the rest of the UTC day.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from ..data.resample import resample
from ..risk.sizing import money_per_price_unit, size_position
from ..schemas.market import Candle, SymbolSpec, Timeframe
from ..strategies.base import Strategy


@dataclass(frozen=True)
class CostModel:
    default_spread_points: float = 25.0
    slippage_points: float = 5.0
    commission_per_lot_side: float = 0.0
    swap_long_per_lot_night: float = -0.0   # account currency per lot per night; ASSUMED unless from broker
    swap_short_per_lot_night: float = -0.0
    latency_bars: int = 0


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_pct: float = 0.5
    max_hold_bars: int = 96
    daily_dd_stop_pct: float = 2.0
    costs: CostModel = field(default_factory=CostModel)


@dataclass
class BtTrade:
    strategy_id: str
    side: str
    signal_index: int
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    target: float
    lots: float
    gross: float
    commission: float
    swap: float
    net: float
    risk_money: float
    r: float
    exit_reason: str
    spread_points_entry: float


@dataclass
class BacktestResult:
    strategy: dict
    trades: list[BtTrade]
    equity_curve: list[float]
    skipped_signals: int
    bars: int
    start: str | None
    end: str | None
    data_is_mock: bool

    def metrics(self) -> dict:
        return compute_metrics(self.trades, self.equity_curve, self.data_is_mock)


def compute_metrics(trades: list[BtTrade] | list[dict], equity_curve: list[float] | None = None,
                    data_is_mock: bool = False) -> dict:
    rs = [t.r if isinstance(t, BtTrade) else t["r"] for t in trades]
    nets = [t.net if isinstance(t, BtTrade) else t["net"] for t in trades]
    n = len(rs)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    max_dd = 0.0
    if equity_curve:
        peak = equity_curve[0]
        for e in equity_curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak * 100 if peak > 0 else 0)
    streak = best = 0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    return {
        "n_trades": n,
        "net_pnl": round(sum(nets), 2),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(gp / gl, 3) if gl > 0 else (None if gp == 0 else float("inf")),
        "expectancy_r": round(sum(rs) / n, 4) if n else None,
        "avg_r": round(sum(rs) / n, 4) if n else None,
        "max_drawdown_pct": round(max_dd, 3),
        "max_consecutive_losses": best,
        "data_is_mock": data_is_mock,
        "sample_warning": None if n >= 30 else f"only {n} trades: statistically insufficient",
    }


def _rollovers(t0, t1) -> int:
    """Number of 21:00 UTC rollovers in (t0, t1], Wednesday counts triple."""
    n = 0
    d = t0.replace(hour=21, minute=0, second=0, microsecond=0)
    if d <= t0:
        d += timedelta(days=1)
    while d <= t1:
        n += 3 if d.weekday() == 2 else 1
        d += timedelta(days=1)
    return n


def run_backtest(strategy: Strategy, m5: list[Candle], spec: SymbolSpec, cfg: BacktestConfig | None = None,
                 data_is_mock: bool = True, start_index: int = 0, end_index: int | None = None,
                 prebuilt: tuple | None = None) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    c = cfg.costs
    if prebuilt is None:
        as_of = m5[-1].open_time_utc + timedelta(minutes=5) if m5 else None
        bars = resample(m5, strategy.exec_tf, as_of) if m5 else []
        htf = {tf: resample(m5, tf, as_of) for tf in strategy.htfs} if m5 else {}
    else:
        bars, htf = prebuilt
    if len(bars) < 50:
        return BacktestResult(strategy.describe(), [], [cfg.initial_equity], 0, len(bars), None, None, data_is_mock)
    ctx = strategy.make_context(bars, htf)
    end_index = len(bars) if end_index is None else min(end_index, len(bars))
    pt = spec.point
    equity = cfg.initial_equity
    curve = [equity]
    trades: list[BtTrade] = []
    skipped = 0
    i = max(start_index, 1)
    day, day_start_eq, day_stopped = None, equity, False
    while i < end_index - 1:
        bar = bars[i]
        if bar.open_time_utc.date() != day:
            day, day_start_eq, day_stopped = bar.open_time_utc.date(), equity, False
        if day_stopped:
            i += 1
            continue
        sig = strategy.signal_at(ctx, i)
        if sig is None:
            i += 1
            continue
        ei = i + 1 + c.latency_bars
        if ei >= end_index:
            break
        eb = bars[ei]
        spread_pts = eb.spread_points if eb.spread_points is not None else c.default_spread_points
        spread = spread_pts * pt
        slip = c.slippage_points * pt
        long = sig.action == "LONG"
        entry = eb.open + spread + slip if long else eb.open - slip
        stop, target = sig.stop_loss, sig.target_for_entry(entry)
        if (long and not stop < entry < target) or (not long and not target < entry < stop):
            skipped += 1
            i += 1
            continue
        lots, rmoney = size_position(spec, equity, cfg.risk_pct, entry, stop)
        if lots <= 0:
            skipped += 1
            i += 1
            continue
        exit_px, reason, xi = None, "TIME_EXIT", min(ei + cfg.max_hold_bars, end_index - 1)
        for j in range(ei, min(ei + cfg.max_hold_bars, end_index - 1) + 1):
            b = bars[j]
            sp = (b.spread_points if b.spread_points is not None else c.default_spread_points) * pt
            if long:
                if b.open <= stop and j > ei:
                    exit_px, reason, xi = b.open - slip, "STOP_LOSS", j
                    break
                if b.low <= stop:
                    exit_px, reason, xi = stop - slip, "STOP_LOSS", j
                    break
                if b.high >= target:
                    exit_px, reason, xi = target, "TAKE_PROFIT", j
                    break
            else:
                if b.open + sp >= stop and j > ei:
                    exit_px, reason, xi = b.open + sp + slip, "STOP_LOSS", j
                    break
                if b.high + sp >= stop:
                    exit_px, reason, xi = stop + slip, "STOP_LOSS", j
                    break
                if b.low + sp <= target:
                    exit_px, reason, xi = target, "TAKE_PROFIT", j
                    break
        if exit_px is None:
            b = bars[xi]
            sp = (b.spread_points if b.spread_points is not None else c.default_spread_points) * pt
            exit_px = b.close if long else b.close + sp
        mpu = money_per_price_unit(spec, lots)
        gross = (exit_px - entry) * mpu if long else (entry - exit_px) * mpu
        comm = 2 * c.commission_per_lot_side * lots
        t_in = eb.open_time_utc
        t_out = bars[xi].open_time_utc + timedelta(seconds=strategy.exec_tf.seconds)
        nights = _rollovers(t_in, t_out)
        swap = nights * lots * (c.swap_long_per_lot_night if long else c.swap_short_per_lot_night)
        net = gross - comm + swap
        equity += net
        curve.append(equity)
        trades.append(BtTrade(strategy.id, sig.action, i, t_in.isoformat(), t_out.isoformat(), round(entry, 6),
                              round(exit_px, 6), stop, target, lots, round(gross, 2), round(comm, 2), round(swap, 2),
                              round(net, 2), round(rmoney, 2), round(net / rmoney, 4) if rmoney else 0.0, reason,
                              spread_pts))
        if (day_start_eq - equity) / day_start_eq * 100 >= cfg.daily_dd_stop_pct:
            day_stopped = True
        i = xi + 1
    return BacktestResult(strategy.describe(), trades, curve, skipped, end_index - start_index,
                          bars[start_index].open_time_utc.isoformat(), bars[end_index - 1].open_time_utc.isoformat(),
                          data_is_mock)


def bootstrap_expectancy(rs: list[float], n_boot: int = 2000, seed: int = 1) -> dict:
    if len(rs) < 10:
        return {"n": len(rs), "ci95": None, "p_mean_le_0": None, "note": "fewer than 10 trades: bootstrap skipped"}
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(rs) for _ in rs) / len(rs) for _ in range(n_boot))
    lo, hi = means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]
    p = sum(1 for m in means if m <= 0) / n_boot
    return {"n": len(rs), "mean_r": round(sum(rs) / len(rs), 4), "ci95": [round(lo, 4), round(hi, 4)],
            "p_mean_le_0": round(p, 4)}


def monte_carlo_drawdown(rs: list[float], risk_pct: float, n_sims: int = 1000, seed: int = 2) -> dict:
    if len(rs) < 10:
        return {"note": "fewer than 10 trades: Monte Carlo skipped"}
    rng = random.Random(seed)
    dds = []
    for _ in range(n_sims):
        eq = peak = 1.0
        dd = 0.0
        for r in rng.sample(rs, len(rs)):
            eq *= 1 + r * risk_pct / 100
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        dds.append(dd * 100)
    dds.sort()
    return {"median_max_dd_pct": round(dds[len(dds) // 2], 3), "p95_max_dd_pct": round(dds[int(0.95 * len(dds))], 3)}


def walk_forward(strategy_cls: type[Strategy], m5: list[Candle], spec: SymbolSpec, cfg: BacktestConfig | None = None,
                 folds: int = 4, base_params: dict | None = None, data_is_mock: bool = True,
                 min_train_trades: int = 5) -> dict:
    """Anchored walk-forward. For fold k: choose params on [0, split_k) by expectancy, test on [split_k, split_{k+1}).

    Parameters are never chosen using test data. Returns OOS metrics + per-fold detail.
    """
    cfg = cfg or BacktestConfig()
    probe = strategy_cls(**(base_params or {}))
    as_of = m5[-1].open_time_utc + timedelta(minutes=5)
    bars = resample(m5, probe.exec_tf, as_of)
    htf = {tf: resample(m5, tf, as_of) for tf in probe.htfs}
    n = len(bars)
    seg = n // (folds + 1)
    if seg < 200:
        return {"error": f"not enough data for {folds} folds ({n} bars)", "oos_metrics": None}
    grid_keys = list(probe.param_grid)
    combos = [{}]
    for k in grid_keys:
        combos = [dict(d, **{k: v}) for d in combos for v in probe.param_grid[k]]
    oos_trades: list[BtTrade] = []
    fold_rows = []
    curve = [cfg.initial_equity]
    for f in range(1, folds + 1):
        tr_end, te_end = seg * f, seg * (f + 1) if f < folds else n
        best, best_score = None, -math.inf
        for combo in combos:
            params = {**(base_params or {}), **combo}
            res = run_backtest(strategy_cls(**params), m5, spec, cfg, data_is_mock, 0, tr_end, prebuilt=(bars, htf))
            m = res.metrics()
            if m["n_trades"] < min_train_trades:
                continue
            score = m["expectancy_r"]
            if score > best_score:
                best, best_score = params, score
        if best is None:
            best = {**(base_params or {})}
        test = run_backtest(strategy_cls(**best), m5, spec, cfg, data_is_mock, tr_end, te_end, prebuilt=(bars, htf))
        tm = test.metrics()
        for t in test.trades:
            curve.append(curve[-1] + t.net)
        oos_trades.extend(test.trades)
        fold_rows.append({"fold": f, "train_bars": tr_end, "test_from": test.start, "test_to": test.end,
                          "chosen_params": best, "train_expectancy_r": None if best_score == -math.inf else round(best_score, 4),
                          "test_metrics": tm})
    oos = compute_metrics(oos_trades, curve, data_is_mock)
    rs = [t.r for t in oos_trades]
    return {"strategy_id": probe.id, "folds": fold_rows, "oos_metrics": oos,
            "bootstrap": bootstrap_expectancy(rs), "monte_carlo": monte_carlo_drawdown(rs, cfg.risk_pct),
            "oos_trades": [asdict(t) for t in oos_trades][-500:], "positive_folds": sum(
                1 for r in fold_rows if (r["test_metrics"]["expectancy_r"] or 0) > 0)}

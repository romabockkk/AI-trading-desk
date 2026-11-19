"""Event-driven bar replay backtester (BACKTESTING.md).

* Uses the SAME ``detect_setup`` rules, the SAME Risk Manager sizing formula and the
  SAME protections as the live desk (NautilusTrader-style backtest/live parity).
* Higher timeframes are resampled from M15 and only *completed* bars are visible.
* Costs: maker fee on limit entries, taker fee + slippage on SL/TP exits, funding.
* Conservative fills: if SL and TP are hit in the same bar, the stop wins.
* Reports in-sample (first 70%) and out-of-sample (last 30%) separately.
  Rules are fixed before the run — nothing is optimised here.
* Not modelled (stated in every report): historical funding/OI filters, news.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..analysis.smc import detect_setup
from ..config import Settings
from ..data.market_data import resample
from ..protections import check_protections
from ..sizing import position_size


@dataclass
class BTTrade:
    direction: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    stop_loss: float
    take_profit: float
    exit: float
    contracts: float
    risk_usd: float
    pnl_usd: float
    fees_usd: float
    funding_usd: float
    result_r: float
    exit_reason: str
    setup_type: str
    equity_before: float
    closed_at: str = ""


def metrics(trades: list[BTTrade], start_equity: float) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.result_r for t in trades])
    pnl = np.array([t.pnl_usd for t in trades])
    eq = start_equity + np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[start_equity], eq]))[1:]
    dd = ((peak - eq) / peak).max() * 100
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    streak = worst = 0
    for x in pnl:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "total_return_pct": float((eq[-1] - start_equity) / start_equity * 100),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "expectancy_r": float(r.mean()),
        "avg_win_r": float(r[r > 0].mean()) if (r > 0).any() else 0.0,
        "avg_loss_r": float(r[r <= 0].mean()) if (r <= 0).any() else 0.0,
        "avg_planned_rr": float(np.mean([abs(t.take_profit - t.entry) / abs(t.entry - t.stop_loss) for t in trades])),
        "max_drawdown_pct": float(dd),
        "max_consecutive_losses": int(worst),
        "fees_usd": float(sum(t.fees_usd + t.funding_usd for t in trades)),
        "net_pnl_usd": float(pnl.sum()),
    }


@dataclass
class BacktestResult:
    inst_id: str
    period: str
    start_equity: float
    trades: list[BTTrade]
    split_time: str
    signals: int
    filtered: dict = field(default_factory=dict)

    def summary(self) -> dict:
        ins = [t for t in self.trades if t.signal_time < self.split_time]
        oos = [t for t in self.trades if t.signal_time >= self.split_time]
        eq_oos = ins[-1].equity_before + ins[-1].pnl_usd if ins else self.start_equity
        by_dir = {d: metrics([t for t in self.trades if t.direction == d], self.start_equity) for d in ("LONG", "SHORT")}
        by_setup = {s: metrics([t for t in self.trades if t.setup_type == s], self.start_equity)
                    for s in sorted({t.setup_type for t in self.trades})}
        return {"inst_id": self.inst_id, "period": self.period, "split_time": self.split_time,
                "signals": self.signals, "filtered": self.filtered,
                "all": metrics(self.trades, self.start_equity),
                "in_sample": metrics(ins, self.start_equity),
                "out_of_sample": metrics(oos, eq_oos),
                "by_direction": by_dir, "by_setup": by_setup,
                "not_modelled": ["historical funding / OI filters", "news & macro events", "partial fills",
                                 "exchange outages"]}


def run_backtest(m15: pd.DataFrame, inst_id: str, instrument: dict, settings: Settings,
                 start_equity: float = 10_000.0, split: float = 0.7) -> BacktestResult:
    p, lim, c = settings.strategy, settings.risk, settings.costs
    ct_val = float(instrument.get("ctVal", 1))
    lot = float(instrument.get("lotSz", 1))
    min_sz = float(instrument.get("minSz", lot))
    h1, h4 = resample(m15, "1h"), resample(m15, "4h")
    t_m15 = m15.index + pd.Timedelta(minutes=15)                 # bar close times
    h1_close = (h1.index + pd.Timedelta(hours=1)).asi8
    h4_close = (h4.index + pd.Timedelta(hours=4)).asi8
    t_ns = t_m15.asi8
    o, hi, lo, cl = (m15[k].to_numpy() for k in ("open", "high", "low", "close"))

    warm = 100 * 16                                                 # 100 H4 bars of history before trading
    split_i = warm + int((len(m15) - warm) * split)
    split_time = t_m15[min(split_i, len(m15) - 1)].isoformat()
    equity = start_equity
    trades: list[BTTrade] = []
    closed_for_prot: list[dict] = []
    pending = None
    position = None
    signals = 0
    last_key = None
    filtered = {"bars_blocked_by_protections": 0, "volatility": 0, "size": 0, "capped_by_margin": 0}

    i = warm
    while i < len(m15):
        now = t_m15[i].to_pydatetime()
        # ----------------------------------------------------- manage position
        if position is not None:
            d = 1 if position["dir"] == "LONG" else -1
            hit_sl = lo[i] <= position["sl"] if d == 1 else hi[i] >= position["sl"]
            hit_tp = (hi[i] >= position["tp"] if d == 1 else lo[i] <= position["tp"]) and i > position["fill_i"]
            if hit_sl or hit_tp:
                if hit_sl:
                    base = position["sl"]
                    gap = o[i] < base if d == 1 else o[i] > base
                    base = o[i] if gap and i > position["fill_i"] else base
                    reason = "stop_loss"
                else:
                    base, reason = position["tp"], "take_profit"
                exit_px = base * (1 - d * c.slippage)
                qty = position["contracts"] * ct_val
                gross = (exit_px - position["entry"]) * d * qty
                fees = position["entry"] * qty * c.maker_fee + exit_px * qty * c.taker_fee
                hours = (now - position["entry_time"]).total_seconds() / 3600
                funding = position["entry"] * qty * c.funding_per_8h * math.floor(hours / 8)
                pnl = gross - fees - funding
                tr = BTTrade(position["dir"], position["signal_time"], position["entry_time"].isoformat(), now.isoformat(),
                             position["entry"], position["sl"], position["tp"], exit_px, position["contracts"],
                             position["risk_usd"], pnl, fees, funding, pnl / position["risk_usd"], reason,
                             position["setup_type"], equity, now.isoformat())
                equity += pnl
                trades.append(tr)
                closed_for_prot.append({"closed_at": now, "pnl_usd": pnl, "equity_before": tr.equity_before})
                position = None
            i += 1
            continue
        # --------------------------------------------------------- pending order
        if pending is not None:
            d = 1 if pending["dir"] == "LONG" else -1
            touched = lo[i] <= pending["entry"] if d == 1 else hi[i] >= pending["entry"]
            if touched:
                fill = min(o[i], pending["entry"]) if d == 1 else max(o[i], pending["entry"])
                position = {**pending, "entry": fill, "fill_i": i, "entry_time": t_m15[i - 1].to_pydatetime()}
                pending = None
                continue                                             # evaluate SL on the fill bar
            if i >= pending["expires_i"]:
                pending = None
            i += 1
            continue
        # ----------------------------------------------------------- new signal
        prot = check_protections(closed_for_prot, now, lim)
        if prot.blocked:
            filtered["bars_blocked_by_protections"] += 1
            i += 1
            continue
        k1 = int(np.searchsorted(h1_close, t_ns[i], side="right"))
        k4 = int(np.searchsorted(h4_close, t_ns[i], side="right"))
        w15 = m15.iloc[max(0, i - 400):i + 1]
        setup = detect_setup(w15, h1.iloc[max(0, k1 - 300):k1], h4.iloc[max(0, k4 - 150):k4], p)
        if setup.status == "READY":
            key = (setup.direction, round(setup.entry, 8), round(setup.stop_loss, 8))
            if key == last_key:
                i += 1
                continue
            last_key = key
            signals += 1
            # Quant filter (same as live QuantAnalyst MAJOR objection): ATR in top 5%
            tr_ = np.maximum(hi[i - 314:i + 1][1:] - lo[i - 314:i + 1][1:],
                             np.abs(np.diff(cl[i - 314:i + 1])))
            atr_s = np.convolve(tr_, np.ones(14) / 14, mode="valid")
            if (atr_s < atr_s[-1]).mean() >= 0.95:
                filtered["volatility"] += 1
                i += 1
                continue
            sz = position_size(setup.entry, setup.stop_loss, equity, equity, lim.risk_per_trade_pct,
                               settings.leverage, ct_val, lot, min_sz, c.taker_fee, c.slippage)
            if not sz.ok:
                filtered["size"] += 1
                i += 1
                continue
            filtered["capped_by_margin"] += int(sz.capped_by_margin)
            contracts = sz.contracts
            pending = {"dir": setup.direction, "entry": setup.entry, "sl": setup.stop_loss, "tp": setup.take_profit,
                       "contracts": contracts, "risk_usd": sz.risk_usd,
                       "expires_i": i + p.entry_valid_bars_m15, "signal_time": t_m15[i].isoformat(),
                       "setup_type": setup.details.get("zone", {}).get("type", "?")}
        i += 1
    period = f"{m15.index[warm].date()} → {m15.index[-1].date()}"
    return BacktestResult(inst_id, period, start_equity, trades, split_time, signals, filtered)


def render_report(summary: dict, trades: list[BTTrade], version: str) -> str:
    def row(name, m):
        if not m.get("trades"):
            return f"| {name} | 0 | – | – | – | – | – | – |"
        return (f"| {name} | {m['trades']} | {m['win_rate']:.0%} | {m['expectancy_r']:+.2f}R | {m['profit_factor']:.2f} "
                f"| {m['total_return_pct']:+.2f}% | {m['max_drawdown_pct']:.2f}% | {m['max_consecutive_losses']} |")
    lines = [f"# Backtest — {summary['inst_id']} — strategy v{version}", "",
             f"Period: {summary['period']} · split (IS/OOS) at {summary['split_time']} · signals {summary['signals']}",
             f"Filtered: {summary['filtered']}", "",
             "| Sample | Trades | Win | Expectancy | PF | Return | Max DD | Max loss streak |",
             "|---|---|---|---|---|---|---|---|",
             row("All", summary["all"]), row("In-sample", summary["in_sample"]), row("Out-of-sample", summary["out_of_sample"]),
             row("LONG", summary["by_direction"]["LONG"]), row("SHORT", summary["by_direction"]["SHORT"])]
    lines += [row(k, v) for k, v in summary["by_setup"].items()]
    lines += ["", "Not modelled: " + ", ".join(summary["not_modelled"]), "",
              "This is historical evidence only; it is not proof of future profitability.", "",
              "## Trades", "", "| Signal | Dir | Entry | SL | TP | Exit | R | Reason |", "|---|---|---|---|---|---|---|---|"]
    lines += [f"| {t.signal_time[:16]} | {t.direction} | {t.entry:g} | {t.stop_loss:g} | {t.take_profit:g} | {t.exit:.6g} "
              f"| {t.result_r:+.2f} | {t.exit_reason} |" for t in trades]
    return "\n".join(lines)


def trades_to_dicts(trades: list[BTTrade]) -> list[dict]:
    return [asdict(t) for t in trades]

"""Deterministic Risk Gate. Output: ALLOW or DENY. No LLM input reaches this module.

Every check runs (no short-circuit) so the decision record lists all failures. Volume is computed here
from the instrument specification and is always rounded DOWN; any volume proposed upstream is ignored.
Missing data of any kind => the corresponding check fails => DENY (NO TRADE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import LoadedRiskLimits, Mode
from ..execution.paper import Instrument
from ..schemas.market import Quote
from ..schemas.trading import Action, GateCheck, GateDecision, GateResult, TradeProposal
from . import sizing


@dataclass(frozen=True)
class OpenPositionRisk:
    symbol: str
    side: str
    risk_money: float
    decision_id: str


@dataclass(frozen=True)
class GateInput:
    mode: Mode
    proposal: TradeProposal
    instrument: Instrument | None          # None = spec incomplete or conversion rate unknown
    spec_missing: tuple[str, ...]
    quote: Quote | None
    account: dict | None                   # PaperBroker.snapshot()
    now_utc: datetime
    consecutive_losses: int
    open_positions: tuple[OpenPositionRisk, ...]
    kill_switch_on: bool
    paused: bool
    market_open: bool
    data_valid: bool
    critical_failures: tuple[str, ...]
    risk_manager_approved: bool
    skeptic_demands_no_trade: bool
    news_blackout: bool | None             # None = UNKNOWN
    atr: float | None
    already_ordered_decision_ids: frozenset[str] = field(default_factory=frozenset)
    last_trade_was_loss: bool = False
    last_risk_pct: float | None = None


def evaluate(inp: GateInput, loaded: LoadedRiskLimits) -> GateDecision:
    L = loaded.limits
    p = inp.proposal
    checks: list[GateCheck] = []

    def chk(name: str, ok: bool, detail: str) -> None:
        checks.append(GateCheck(name=name, passed=bool(ok), detail=detail))

    is_trade = p.action in (Action.LONG, Action.SHORT)
    chk("ACTION_IS_TRADE", is_trade, f"action={p.action.value}")
    chk("MODE_IS_PAPER", inp.mode is Mode.PAPER, f"mode={inp.mode.value}")
    chk("KILL_SWITCH_OFF", not inp.kill_switch_on, "kill switch ON" if inp.kill_switch_on else "off")
    chk("NOT_PAUSED", not inp.paused, "new trades paused" if inp.paused else "running")
    chk("MARKET_OPEN", inp.market_open, "market open" if inp.market_open else "market closed / no fresh tradeable quote")
    chk("CRITICAL_AGENTS_OK", not inp.critical_failures,
        "all critical agents OK" if not inp.critical_failures else f"failed: {list(inp.critical_failures)}")
    chk("RISK_MANAGER_APPROVED", inp.risk_manager_approved, "approved" if inp.risk_manager_approved else "VETO")
    chk("SKEPTIC_NO_DEMAND", not inp.skeptic_demands_no_trade,
        "no demand" if not inp.skeptic_demands_no_trade else "skeptic demands NO_TRADE")
    chk("DATA_VALID", inp.data_valid, "validated" if inp.data_valid else "market data invalid or missing")

    inst = inp.instrument
    chk("SPEC_COMPLETE", inst is not None,
        "complete" if inst is not None else f"missing spec fields / conversion: {list(inp.spec_missing) or ['conversion rate']}")
    spec = inst.spec if inst else None

    q = inp.quote
    q_age = (inp.now_utc - q.time_utc).total_seconds() if q else None
    chk("QUOTE_FRESH", q is not None and -5 <= q_age <= L.max_quote_age_seconds,
        f"age={q_age:.1f}s (max {L.max_quote_age_seconds}s)" if q_age is not None else "no quote")
    chk("QUOTE_REAL", q is not None and not q.source.is_mock, f"source={q.source.name}" if q else "no quote")
    spread_pts = q.spread_points(spec.point) if (q and spec) else None
    chk("SPREAD_LIMIT", spread_pts is not None and spread_pts <= L.max_spread_points,
        f"spread={spread_pts} points (max {L.max_spread_points})")

    has_sl = is_trade and p.stop_loss is not None and p.entry is not None
    chk("STOP_LOSS_PRESENT", has_sl, "stop-loss set" if has_sl else "missing stop-loss")
    equity = inp.account["equity"] if inp.account else None
    chk("ACCOUNT_KNOWN", equity is not None and equity > 0, f"equity={equity}")

    lots = 0.0
    rmoney = 0.0
    if has_sl and inst is not None and q is not None:
        entry = q.ask if p.action is Action.LONG else q.bid          # realistic entry, not the proposal
        stop_dist = abs(entry - p.stop_loss)
        side_ok = (p.stop_loss < entry) if p.action is Action.LONG else (p.stop_loss > entry)
        chk("STOP_ON_CORRECT_SIDE", side_ok, f"entry={entry} sl={p.stop_loss}")
        spread_price = q.ask - q.bid
        ratio = spread_price / stop_dist if stop_dist > 0 else float("inf")
        chk("SPREAD_TO_STOP_RATIO", ratio <= L.max_spread_to_stop_ratio, f"{ratio:.3f} (max {L.max_spread_to_stop_ratio})")
        if inp.atr:
            sr = spread_price / inp.atr
            chk("SPREAD_TO_ATR_RATIO", sr <= L.max_spread_to_atr_ratio, f"{sr:.3f} of ATR (max {L.max_spread_to_atr_ratio})")
        if inp.atr:
            dev = abs(entry - p.entry) / inp.atr
            chk("ENTRY_NEAR_QUOTE", dev <= L.max_entry_deviation_atr, f"{dev:.2f} ATR from proposal")
        else:
            chk("ENTRY_NEAR_QUOTE", False, "ATR unknown")
        if p.take_profit is not None and side_ok and stop_dist > 0:
            rr = abs(p.take_profit - entry) / stop_dist
            tp_side = (p.take_profit > entry) if p.action is Action.LONG else (p.take_profit < entry)
            chk("REWARD_RISK", tp_side and rr >= L.min_reward_risk, f"RR={rr:.2f} at live price (min {L.min_reward_risk})")
        else:
            chk("REWARD_RISK", False, "no take-profit or invalid geometry")
        if equity and equity > 0 and side_ok:
            budget = equity * L.max_risk_per_trade_pct / 100
            lots, rmoney = sizing.size_position(spec, equity, L.max_risk_per_trade_pct, entry, p.stop_loss, spread_price)
            min_risk = sizing.min_lot_risk(spec, entry, p.stop_loss, spread_price)
            chk("MIN_VOLUME_WITHIN_RISK", lots >= spec.volume_min,
                f"min volume {spec.volume_min} lot risks ${min_risk:.4f} incl. spread+commission; budget ${budget:.4f}"
                if lots < spec.volume_min else f"lots={lots} (rounded down, step {spec.volume_step})")
            risk_pct = rmoney / equity * 100 if equity else 0
            chk("RISK_PER_TRADE", 0 < risk_pct <= L.max_risk_per_trade_pct + 1e-9,
                f"{risk_pct:.3f}% (max {L.max_risk_per_trade_pct}%)")
            open_risk = sum(x.risk_money for x in inp.open_positions)
            tot_pct = (open_risk + rmoney) / equity * 100
            chk("TOTAL_OPEN_RISK", tot_pct <= L.max_total_open_risk_pct + 1e-9,
                f"{tot_pct:.3f}% (max {L.max_total_open_risk_pct}%)")
            use_lots = lots if lots > 0 else spec.volume_min
            new_margin = inst.margin(use_lots, entry)
            comm = inst.commission(use_lots)
            used = inp.account["margin"] + new_margin
            free_after = inp.account["free_margin"] - new_margin - comm
            chk("FREE_MARGIN", free_after > 0, f"free margin after open ${free_after:.4f} (margin ${new_margin:.4f}, commission ${comm:.4f})")
            usage = used / equity * 100
            chk("MARGIN_USAGE", usage <= L.max_margin_usage_pct, f"{usage:.2f}% of equity (max {L.max_margin_usage_pct}%)")
            level = equity / used * 100 if used > 0 else float("inf")
            chk("MARGIN_LEVEL", level >= L.min_margin_level_pct, f"{level:.0f}% (min {L.min_margin_level_pct}%)")
            eff = inst.exposure_usd(use_lots, entry) / equity
            chk("EFFECTIVE_LEVERAGE", eff <= L.max_effective_leverage, f"{eff:.2f}x (max {L.max_effective_leverage}x)")
            if inp.last_trade_was_loss and inp.last_risk_pct is not None:
                chk("NO_RISK_INCREASE_AFTER_LOSS", risk_pct <= inp.last_risk_pct + 1e-6,
                    f"{risk_pct:.3f}% vs previous {inp.last_risk_pct:.3f}%")
    elif is_trade:
        chk("MIN_VOLUME_WITHIN_RISK", False, "cannot size: missing stop, complete spec or quote")

    chk("MAX_CONCURRENT_POSITIONS", len(inp.open_positions) < L.max_concurrent_positions,
        f"{len(inp.open_positions)} open (max {L.max_concurrent_positions})")
    same = [x for x in inp.open_positions if x.symbol == p.symbol]
    chk("NO_AVERAGING_OR_STACKING", not same, "no position in symbol" if not same else "position already open in symbol")
    chk("NO_DUPLICATE_ORDER", p.decision_id not in inp.already_ordered_decision_ids, f"decision {p.decision_id}")

    if inp.account:
        dd = -inp.account["daily_pnl"] / inp.account["day_start_equity"] * 100 if inp.account["day_start_equity"] else 0
        chk("DAILY_LOSS_LIMIT", dd < L.max_daily_drawdown_pct, f"today {dd:.2f}% loss (stop at {L.max_daily_drawdown_pct}%)")
    else:
        chk("DAILY_LOSS_LIMIT", False, "account unknown")
    if inp.account:
        a = inp.account
        wk = -a.get("weekly_pnl", 0.0) / a["week_start_equity"] * 100 if a.get("week_start_equity") else 0.0
        chk("WEEKLY_LOSS_LIMIT", wk < L.max_weekly_drawdown_pct, f"this week {wk:.2f}% loss (stop at {L.max_weekly_drawdown_pct}%)")
        tot = (a["initial_balance"] - a["equity"]) / a["initial_balance"] * 100 if a.get("initial_balance") else 0.0
        chk("TOTAL_DRAWDOWN_LIMIT", tot < L.max_total_drawdown_pct,
            f"{tot:.2f}% below initial balance (circuit breaker at {L.max_total_drawdown_pct}%)")
        n = a.get("trades_today", 0)
        chk("TRADES_PER_DAY", n < L.max_trades_per_day, f"{n} trades today (max {L.max_trades_per_day})")
        chk("LOSS_STREAK", inp.consecutive_losses < L.max_consecutive_losses,
        f"{inp.consecutive_losses} consecutive losses (pause at {L.max_consecutive_losses})")

    if inp.news_blackout is True:
        chk("NEWS", False, "inside high-impact news blackout")
    elif inp.news_blackout is None:
        allow = L.news_unknown_policy == "ALLOW_PAPER_ONLY"
        chk("NEWS", allow, "news status UNKNOWN" + (" (allowed in PAPER by owner policy)" if allow else ""))
    else:
        chk("NEWS", True, "no blackout")

    failed = [c for c in checks if not c.passed]
    return GateDecision(result=GateResult.DENY if failed else GateResult.ALLOW, checks=tuple(checks),
                        volume_lots=lots if not failed else None, risk_money=rmoney if not failed else None,
                        risk_limits_sha256=loaded.sha256)

"""Deterministic Risk Gate. Output: ALLOW or DENY. No LLM input reaches this module.

Every check runs (no short-circuit) so the decision record lists all failures.
Volume is computed here from the contract spec; any volume proposed upstream is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import LoadedRiskLimits, Mode
from ..schemas.market import AccountInfo, Quote, SymbolSpec
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
    spec: SymbolSpec | None
    quote: Quote | None
    account: AccountInfo | None
    now_utc: datetime
    day_start_equity: float | None
    consecutive_losses: int
    open_positions: tuple[OpenPositionRisk, ...]
    kill_switch_on: bool
    paused: bool
    market_open: bool
    data_valid: bool
    critical_failures: tuple[str, ...]
    risk_manager_approved: bool
    skeptic_demands_no_trade: bool
    news_blackout: bool | None            # None = UNKNOWN
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
    chk("MODE_ALLOWS_EXECUTION", inp.mode in (Mode.PAPER, Mode.BROKER_DEMO), f"mode={inp.mode.value}")
    chk("KILL_SWITCH_OFF", not inp.kill_switch_on, "kill switch ON" if inp.kill_switch_on else "off")
    chk("NOT_PAUSED", not inp.paused, "paused" if inp.paused else "running")
    chk("MARKET_OPEN", inp.market_open, "market open" if inp.market_open else "market closed")
    chk("CRITICAL_AGENTS_OK", not inp.critical_failures,
        "all critical agents OK" if not inp.critical_failures else f"failed: {list(inp.critical_failures)}")
    chk("RISK_MANAGER_APPROVED", inp.risk_manager_approved, "approved" if inp.risk_manager_approved else "VETO")
    chk("SKEPTIC_NO_DEMAND", not inp.skeptic_demands_no_trade,
        "no demand" if not inp.skeptic_demands_no_trade else "skeptic demands NO_TRADE")
    chk("DATA_VALID", inp.data_valid, "validated" if inp.data_valid else "market data invalid")

    spec = inp.spec
    chk("SPEC_AVAILABLE", spec is not None, "spec loaded" if spec else "contract specification unavailable")
    if spec is not None and inp.mode is Mode.BROKER_DEMO:
        chk("SPEC_FROM_BROKER", not spec.source.is_mock, f"source={spec.source.name}")
    tick_ok = spec is not None and spec.tick_value > 0 and spec.tick_size > 0
    chk("TICK_VALUE_KNOWN", tick_ok, f"tick_value={getattr(spec, 'tick_value', None)}")
    chk("SPEC_TRADE_ALLOWED", spec is not None and spec.trade_allowed, "trade allowed" if spec and spec.trade_allowed else "not tradeable")

    q = inp.quote
    q_age = (inp.now_utc - q.time_utc).total_seconds() if q else None
    chk("QUOTE_FRESH", q is not None and -5 <= q_age <= L.max_quote_age_seconds,
        f"age={q_age:.1f}s" if q_age is not None else "no quote")
    if inp.mode is Mode.BROKER_DEMO and q is not None:
        chk("QUOTE_FROM_BROKER", not q.source.is_mock, f"source={q.source.name}")
    spread_pts = q.spread_points(spec.point) if (q and spec) else None
    chk("SPREAD_LIMIT", spread_pts is not None and spread_pts <= L.max_spread_points,
        f"spread={spread_pts} points (max {L.max_spread_points})")

    has_sl = is_trade and p.stop_loss is not None and p.entry is not None
    chk("STOP_LOSS_PRESENT", has_sl, "stop-loss set" if has_sl else "missing stop-loss")

    lots = 0.0
    rmoney = 0.0
    equity = inp.account.equity if inp.account else None
    chk("ACCOUNT_KNOWN", inp.account is not None and equity is not None and equity > 0, f"equity={equity}")

    if has_sl and spec is not None and q is not None:
        entry = q.ask if p.action is Action.LONG else q.bid          # realistic entry, not the proposal
        stop_dist = abs(entry - p.stop_loss)
        side_ok = (p.stop_loss < entry) if p.action is Action.LONG else (p.stop_loss > entry)
        chk("STOP_ON_CORRECT_SIDE", side_ok, f"entry={entry} sl={p.stop_loss}")
        min_stop = spec.stops_level_points * spec.point
        chk("STOP_DISTANCE_VS_STOPS_LEVEL", stop_dist > min_stop, f"dist={stop_dist:.5f} min={min_stop:.5f}")
        spread_price = q.ask - q.bid
        ratio = spread_price / stop_dist if stop_dist > 0 else float("inf")
        chk("SPREAD_TO_STOP_RATIO", ratio <= L.max_spread_to_stop_ratio, f"{ratio:.3f} (max {L.max_spread_to_stop_ratio})")
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
            lots, rmoney = sizing.size_position(spec, equity, L.max_risk_per_trade_pct, entry, p.stop_loss, spread_price)
            chk("POSITION_SIZE_VALID", lots >= spec.volume_min,
                f"lots={lots} (min {spec.volume_min}, step {spec.volume_step})")
            risk_pct = rmoney / equity * 100 if equity else 0
            chk("RISK_PER_TRADE", 0 < risk_pct <= L.max_risk_per_trade_pct + 1e-9,
                f"{risk_pct:.3f}% (max {L.max_risk_per_trade_pct}%)")
            open_risk = sum(x.risk_money for x in inp.open_positions)
            tot_pct = (open_risk + rmoney) / equity * 100
            chk("TOTAL_OPEN_RISK", tot_pct <= L.max_total_open_risk_pct + 1e-9,
                f"{tot_pct:.3f}% (max {L.max_total_open_risk_pct}%)")
            lev = inp.account.leverage if inp.account else 1
            new_margin = sizing.required_margin(spec, lots, entry, lev, inp.account.currency if inp.account else "USD")
            used = (inp.account.margin if inp.account else 0) + new_margin
            usage = used / equity * 100
            chk("MARGIN_USAGE", usage <= L.max_margin_usage_pct, f"{usage:.2f}% (max {L.max_margin_usage_pct}%)")
            level = equity / used * 100 if used > 0 else float("inf")
            chk("MARGIN_LEVEL", level >= L.min_margin_level_pct, f"{level:.0f}% (min {L.min_margin_level_pct}%)")
            notional_total = sizing.notional(spec, lots, entry)
            eff = notional_total / equity
            chk("EFFECTIVE_LEVERAGE", eff <= L.max_effective_leverage, f"{eff:.2f}x (max {L.max_effective_leverage}x)")
            if inp.last_trade_was_loss and inp.last_risk_pct is not None:
                chk("NO_RISK_INCREASE_AFTER_LOSS", risk_pct <= inp.last_risk_pct + 1e-6,
                    f"{risk_pct:.3f}% vs previous {inp.last_risk_pct:.3f}%")
    elif is_trade:
        chk("POSITION_SIZE_VALID", False, "cannot size: missing stop, spec or quote")

    chk("MAX_CONCURRENT_POSITIONS", len(inp.open_positions) < L.max_concurrent_positions,
        f"{len(inp.open_positions)} open (max {L.max_concurrent_positions})")
    same_dir = [x for x in inp.open_positions if x.symbol == p.symbol]
    chk("NO_AVERAGING_OR_STACKING", not same_dir, "no position in symbol" if not same_dir else "position already open in symbol")
    chk("NO_DUPLICATE_ORDER", p.decision_id not in inp.already_ordered_decision_ids, f"decision {p.decision_id}")

    if inp.day_start_equity and equity:
        dd = (inp.day_start_equity - equity) / inp.day_start_equity * 100
        chk("DAILY_DRAWDOWN", dd < L.max_daily_drawdown_pct, f"{dd:.2f}% (stop at {L.max_daily_drawdown_pct}%)")
    else:
        chk("DAILY_DRAWDOWN", False, "day start equity unknown")
    chk("LOSS_STREAK", inp.consecutive_losses < L.max_consecutive_losses,
        f"{inp.consecutive_losses} consecutive losses (pause at {L.max_consecutive_losses})")

    if inp.news_blackout is True:
        chk("NEWS", False, "inside high-impact news blackout")
    elif inp.news_blackout is None:
        allow = inp.mode is Mode.PAPER and L.news_unknown_policy == "ALLOW_PAPER_ONLY"
        chk("NEWS", allow, "news status UNKNOWN" + (" (allowed in PAPER by owner policy)" if allow else ""))
    else:
        chk("NEWS", True, "no blackout")

    failed = [c for c in checks if not c.passed]
    return GateDecision(result=GateResult.DENY if failed else GateResult.ALLOW, checks=tuple(checks),
                        volume_lots=lots if not failed else None, risk_money=rmoney if not failed else None,
                        risk_limits_sha256=loaded.sha256)

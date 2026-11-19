"""Chief and the seven agents. Core logic is deterministic and reproducible; an optional LLM review
can only add caution. Agents exchange information only through persisted structured messages."""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..analysis import structure as ms
from ..analysis.indicators import efficiency_ratio
from ..config import PROJECT_ROOT, LoadedRiskLimits, Mode, Settings
from ..data.service import MarketSnapshot
from ..risk import sizing
from ..schemas.agents import AgentName, AgentReport, ReportStatus, SourceRef
from ..schemas.common import utcnow
from ..schemas.market import AccountInfo, Timeframe
from ..strategies.base import Strategy
from .base import AgentOutput, BaseAgent

CORRELATED = {"XAUUSD": {"XAGUSD"}, "XAGUSD": {"XAUUSD"}, "EURUSD": {"GBPUSD"}, "GBPUSD": {"EURUSD"}}
SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass
class ActiveStrategy:
    strategy: Strategy
    version: str
    evidence: dict | None          # latest OOS experiment summary for this version
    experiment_id: str | None


@dataclass
class CycleContext:
    conn: sqlite3.Connection
    settings: Settings
    limits: LoadedRiskLimits
    snapshot: MarketSnapshot
    now: datetime
    active: list[ActiveStrategy]
    research: list[ActiveStrategy]
    account: AccountInfo | None
    open_positions: list[dict]
    consecutive_losses: int
    day_start_equity: float | None
    calendar_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "calendar.csv")
    reports: dict[AgentName, AgentReport] = field(default_factory=dict)
    proposal: dict | None = None
    debate: list[dict] = field(default_factory=list)

    def payload(self, a: AgentName) -> dict | None:
        r = self.reports.get(a)
        return r.payload if r and r.status is ReportStatus.OK else None


# ---------------------------------------------------------------- analysts
class MarketStructureAgent(BaseAgent):
    name = AgentName.MARKET_STRUCTURE

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        s = ctx.snapshot
        bias, events = {}, []
        for tf in (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5):
            bars = s.bars.get(tf, [])
            res = ms.analyze(bars, k=2)
            bias[tf.value] = ms.bias(res, len(bars) - 1) if bars else "UNKNOWN"
            t = lambda i: bars[i].open_time_utc.isoformat()  # noqa: E731
            for b in res.breaks[-2:]:
                events.append({"timeframe": tf.value, "kind": b.kind, "direction": b.direction, "time_utc": t(b.index),
                               "price_low": b.level, "price_high": b.level, "rule_id": f"MS.{b.kind}.v1"})
            for sw in res.sweeps[-1:]:
                events.append({"timeframe": tf.value, "kind": "LIQUIDITY_SWEEP", "direction": sw.direction,
                               "time_utc": t(sw.index), "price_low": sw.level, "price_high": sw.level, "rule_id": "MS.SWEEP.v1"})
            for ob in [o for o in res.order_blocks if o.invalidated_at is None][-1:]:
                events.append({"timeframe": tf.value, "kind": "ORDER_BLOCK", "direction": ob.direction, "time_utc": t(ob.index),
                               "price_low": ob.low, "price_high": ob.high, "rule_id": "MS.OB.v1"})
            for fv in res.fvgs[-1:]:
                events.append({"timeframe": tf.value, "kind": "FVG", "direction": fv.direction, "time_utc": t(fv.index),
                               "price_low": fv.low, "price_high": fv.high, "rule_id": "MS.FVG.v1"})
            sw_lab = [x for x in res.swings if x.label][-2:]
            for x in sw_lab:
                events.append({"timeframe": tf.value, "kind": x.label, "direction": "NONE", "time_utc": t(x.index),
                               "price_low": x.price, "price_high": x.price, "rule_id": "MS.LABEL.v1"})
        aligned = len({v for v in (bias["H4"], bias["H1"]) if v != "UNKNOWN"}) == 1
        summary = (f"H4 {bias['H4']}, H1 {bias['H1']}, M15 {bias['M15']}, M5 {bias['M5']}; "
                   f"{'H4/H1 aligned' if aligned else 'H4/H1 not aligned'}"
                   + ("; DATA IS MOCK" if s.is_mock else ""))
        return AgentOutput({"bias_by_timeframe": bias, "events": events, "summary": summary},
                           confidence=0.6 if aligned else 0.3, snapshot_ids=[s.snapshot_id])

    def respond(self, ctx: CycleContext, obj: dict) -> dict:
        p = ctx.payload(self.name) or {}
        b = p.get("bias_by_timeframe", {})
        sig = (ctx.proposal or {}).get("signal") or {}
        want = "BULLISH" if sig.get("action") == "LONG" else "BEARISH"
        ok = b.get("H4") == want
        return {"objection_id": obj["id"], "resolved": ok,
                "facts": [f"H4 bias={b.get('H4')}, H1={b.get('H1')}, M15={b.get('M15')}; trade wants {want}",
                          "H1 counter-move inside an aligned H4 trend is treated as a pullback" if ok else
                          "H4 bias does not support the trade direction"]}


class QuantAgent(BaseAgent):
    name = AgentName.QUANT

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        s = ctx.snapshot
        m15 = s.bars.get(Timeframe.M15, [])
        closes = [b.close for b in m15]
        er = efficiency_ratio(closes, 50, len(closes) - 1) if len(closes) > 51 else None
        regime = "UNKNOWN"
        if er is not None:
            regime = "TRENDING" if er >= 0.3 else ("RANGING" if er <= 0.15 else "UNKNOWN")
        signals, exp_ids = [], []
        for group, active in ((ctx.active, True), (ctx.research, False)):
            for a in group:
                st = a.strategy
                bars = s.bars.get(st.exec_tf, [])
                if len(bars) < 60:
                    continue
                c = st.make_context(bars, {tf: s.bars.get(tf, []) for tf in st.htfs})
                sig = st.signal_at(c, len(bars) - 1)
                if a.experiment_id:
                    exp_ids.append(a.experiment_id)
                if sig is None:
                    continue
                signals.append({"strategy_id": st.id, "version": a.version, "active": active, "action": sig.action,
                                "entry": sig.ref_price, "sl": sig.stop_loss, "tp": sig.take_profit,
                                "planned_rr": round(sig.planned_rr, 3), "rr_target": sig.rr_target, "reason": sig.reason, "invalidation": sig.invalidation,
                                "rule_ids": list(sig.rule_ids), "signal_bar_utc": sig.time_utc,
                                "experiment_id": a.experiment_id, "oos_metrics_ref": a.evidence})
        summary = (f"regime={regime} (ER50={er:.2f})" if er is not None else "regime UNKNOWN") + \
                  f"; {sum(1 for x in signals if x['active'])} active-strategy signals, " \
                  f"{sum(1 for x in signals if not x['active'])} research-only signals"
        return AgentOutput({"regime": regime, "signals": signals, "evidence_experiment_ids": sorted(set(exp_ids)),
                            "summary": summary}, confidence=None, snapshot_ids=[s.snapshot_id])

    def respond(self, ctx: CycleContext, obj: dict) -> dict:
        sig = (ctx.proposal or {}).get("signal") or {}
        ev = sig.get("oos_metrics_ref") or {}
        oos = ev.get("oos_metrics") or {}
        bs = ev.get("bootstrap") or {}
        n = oos.get("n_trades") or 0
        ci = bs.get("ci95")
        folds_ok = ev.get("positive_folds", 0) >= max(1, ev.get("n_folds", 4)) / 2
        ok = n >= 30 and ci is not None and ci[0] > 0 and folds_ok
        return {"objection_id": obj["id"], "resolved": ok,
                "facts": [f"OOS trades={n}, expectancy={oos.get('expectancy_r')}, CI95={ci}, "
                          f"positive folds={ev.get('positive_folds')}/{ev.get('n_folds')}, mock={oos.get('data_is_mock')}"]}


class MacroAgent(BaseAgent):
    name = AgentName.MACRO

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        now = ctx.now
        path = ctx.calendar_path
        sym = ctx.snapshot.symbol
        ccys = {"USD"} if sym.startswith(("XAU", "XAG", "US500")) else {sym[:3], sym[3:6]}
        if not path.exists():
            return AgentOutput({"usd_bias": "UNKNOWN", "gold_bias": "UNKNOWN", "upcoming_events": [],
                                "blackout_active": None, "data_status": "UNKNOWN",
                                "summary": "UNKNOWN: no economic calendar source connected (data/calendar.csv absent)"})
        events, blackout = [], False
        win = timedelta(minutes=ctx.limits.limits.news_blackout_minutes)
        retrieved = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    t = datetime.fromisoformat(row["scheduled_utc"].replace("Z", "+00:00"))
                    pub = row.get("published_utc") or ""
                    pub_t = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
                except (KeyError, ValueError):
                    continue
                if not row.get("source_name"):
                    continue          # unsourced rows are ignored
                if row.get("currency", "").upper() not in ccys:
                    continue
                if not (now - timedelta(hours=2) <= t <= now + timedelta(hours=24)):
                    continue
                impact = row.get("impact", "UNKNOWN").upper()
                impact = impact if impact in ("HIGH", "MEDIUM", "LOW") else "UNKNOWN"
                events.append({"name": row.get("event", "?"), "scheduled_utc": t.isoformat(), "impact": impact,
                               "source": {"name": row["source_name"], "url": row.get("source_url") or None,
                                          "published_utc": pub_t.isoformat() if pub_t else None,
                                          "retrieved_utc": retrieved.isoformat()}})
                if impact in ("HIGH", "UNKNOWN") and abs(t - now) <= win:
                    blackout = True
        return AgentOutput({"usd_bias": "UNKNOWN", "gold_bias": "UNKNOWN", "upcoming_events": events,
                            "blackout_active": blackout, "data_status": "PARTIAL",
                            "summary": f"{len(events)} calendar events in window; blackout={'YES' if blackout else 'no'}; "
                                       "directional macro bias UNKNOWN (no rates/yields/DXY feed connected)"},
                           sources=[SourceRef(name=f"owner calendar file {path.name}", retrieved_utc=retrieved)])

    def respond(self, ctx: CycleContext, obj: dict) -> dict:
        p = ctx.payload(self.name) or {}
        b = p.get("blackout_active")
        return {"objection_id": obj["id"], "resolved": b is False,
                "facts": [f"blackout_active={b}, data_status={p.get('data_status')}"]}


# ---------------------------------------------------------------- chief
class ChiefAgent(BaseAgent):
    name = AgentName.CHIEF

    def hypothesis(self, ctx: CycleContext) -> dict:
        q = ctx.payload(AgentName.QUANT) or {}
        msr = ctx.payload(AgentName.MARKET_STRUCTURE) or {}
        h4 = (msr.get("bias_by_timeframe") or {}).get("H4", "UNKNOWN")
        cands = []
        for sgn in q.get("signals", []):
            if not sgn["active"]:
                continue
            want = "BULLISH" if sgn["action"] == "LONG" else "BEARISH"
            if h4 not in ("UNKNOWN", want):
                continue
            exp = ((sgn.get("oos_metrics_ref") or {}).get("oos_metrics") or {}).get("expectancy_r")
            cands.append((exp if exp is not None else -9, sgn))
        if not cands:
            reason = ("no active (owner-approved) strategy produced a signal on the last closed bar"
                      if not [s for s in q.get("signals", []) if s["active"]] else
                      "active signals conflict with H4 structure bias")
            return {"hypothesis": f"NO_TRADE: {reason}", "alternative": "wait for next closed bar",
                    "proposed_action": "NO_TRADE", "signal": None, "reason": reason}
        cands.sort(key=lambda x: x[0], reverse=True)
        sig = cands[0][1]
        opp = "SHORT" if sig["action"] == "LONG" else "LONG"
        return {"hypothesis": f"{sig['action']} {ctx.snapshot.symbol} via {sig['strategy_id']} v{sig['version']}: {sig['reason']}",
                "alternative": f"NO_TRADE or {opp} if invalidated ({sig['invalidation']})",
                "proposed_action": sig["action"], "signal": sig, "reason": sig["reason"]}

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        prop = ctx.proposal or self.hypothesis(ctx)
        unresolved = [m for m in ctx.debate if m["message_type"] == "OBJECTION" and not m.get("resolved")
                      and SEVERITY_ORDER[m["payload"]["severity"]] >= 2]
        demand = any(m["message_type"] == "DEMAND_NO_TRADE" for m in ctx.debate)
        reasons = [prop["reason"]]
        final = prop["proposed_action"]
        if demand:
            final, reasons = "NO_TRADE", reasons + ["Skeptic demanded NO_TRADE (critical objection)"]
        elif unresolved:
            final, reasons = "NO_TRADE", reasons + [f"{len(unresolved)} unresolved HIGH/CRITICAL objections"]
        return AgentOutput({"hypothesis": prop["hypothesis"], "alternative": prop["alternative"],
                            "proposed_action": prop["proposed_action"], "final_action": final, "reasons": reasons,
                            "unresolved_objection_ids": [m["payload"]["id"] for m in unresolved]})

    def respond(self, ctx: CycleContext, obj: dict) -> dict:
        if obj["category"] == "DATA" and ctx.settings.mode is Mode.PAPER and ctx.snapshot.valid:
            return {"objection_id": obj["id"], "resolved": True,
                    "facts": ["PAPER mode: mock data permitted for pipeline testing only; every result is flagged MOCK"]}
        return {"objection_id": obj["id"], "resolved": False, "facts": ["no counter-evidence available"]}


# ---------------------------------------------------------------- skeptic
class SkepticAgent(BaseAgent):
    name = AgentName.SKEPTIC

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        objs: list[dict] = []
        n = 0

        relax = ctx.settings.mode is Mode.PAPER and ctx.limits.limits.paper_allow_unproven_strategies

        def add(cat, text, sev):
            nonlocal n
            n += 1
            if relax and cat in ("STATISTICS", "OVERFITTING") and sev == "HIGH":
                sev, text = "MEDIUM", text + " [downgraded: owner policy paper_allow_unproven_strategies]"
            objs.append({"id": f"OBJ-{n}", "category": cat, "text": text, "severity": sev})

        s = ctx.snapshot
        prop = ctx.proposal or {}
        sig = prop.get("signal")
        if not s.valid:
            add("DATA", f"market data invalid: {[i['code'] for i in s.issues if i['level']=='ERROR'][:5]}", "CRITICAL")
        if s.is_mock:
            add("DATA", "snapshot source is MOCK (synthetic); conclusions do not transfer to real markets",
                "CRITICAL" if ctx.settings.mode is Mode.BROKER_DEMO else "MEDIUM")
        macro = ctx.payload(AgentName.MACRO)
        if macro is None:
            add("NEWS", "Macro report missing", "CRITICAL")
        elif macro["blackout_active"] is True:
            add("NEWS", "inside high-impact news blackout window", "CRITICAL")
        elif macro["blackout_active"] is None:
            add("NEWS", "news calendar UNKNOWN: cannot exclude CPI/NFP/FOMC risk", "MEDIUM")
        if sig:
            ev = sig.get("oos_metrics_ref") or {}
            oos = ev.get("oos_metrics") or {}
            if not oos:
                add("STATISTICS", f"{sig['strategy_id']} v{sig['version']} has no out-of-sample evidence", "HIGH")
            else:
                if (oos.get("n_trades") or 0) < 30:
                    add("STATISTICS", f"OOS sample only {oos.get('n_trades')} trades (<30)", "HIGH")
                ci = (ev.get("bootstrap") or {}).get("ci95")
                if not ci or ci[0] <= 0:
                    add("STATISTICS", f"bootstrap CI95 of OOS expectancy {ci} includes <= 0", "HIGH")
                if ev.get("n_folds") and ev.get("positive_folds", 0) < ev["n_folds"] / 2:
                    add("OVERFITTING", f"only {ev.get('positive_folds')}/{ev['n_folds']} walk-forward folds positive", "HIGH")
            msr = ctx.payload(AgentName.MARKET_STRUCTURE) or {}
            b = msr.get("bias_by_timeframe", {})
            if b.get("H4") != b.get("H1"):
                add("STRUCTURE", f"H4 {b.get('H4')} vs H1 {b.get('H1')} disagree", "MEDIUM")
            q = ctx.payload(AgentName.QUANT) or {}
            opp = [x for x in q.get("signals", []) if x["active"] and x["action"] != sig["action"]]
            if opp:
                add("CONTRADICTION", f"active strategies disagree: {[x['strategy_id'] for x in opp]}", "HIGH")
            corr = CORRELATED.get(s.symbol, set())
            if any(p["symbol"] in corr for p in ctx.open_positions):
                add("CORRELATION", f"open position in correlated instrument {corr}", "HIGH")
            if s.quote and s.spec:
                sp = s.quote.spread_points(s.spec.point)
                if sp > 0.6 * ctx.limits.limits.max_spread_points:
                    add("EXECUTION", f"spread {sp} points is >60% of limit", "MEDIUM")
        demand = any(o["severity"] == "CRITICAL" for o in objs)
        return AgentOutput({"objections": objs, "demand_no_trade": demand,
                            "summary": f"{len(objs)} objections; demand NO_TRADE={demand}"})


# ---------------------------------------------------------------- risk manager
class RiskManagerAgent(BaseAgent):
    name = AgentName.RISK_MANAGER

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        L = ctx.limits.limits
        sig = (ctx.proposal or {}).get("signal")
        s = ctx.snapshot
        reasons: list[str] = []
        if not sig:
            return AgentOutput({"verdict": "VETO", "volume_lots": None, "risk_money": None, "risk_pct": None,
                                "reasons": ["no trade proposed"]})
        if ctx.account is None:
            reasons.append("account unknown")
        if s.spec is None or s.quote is None:
            reasons.append("spec or quote unavailable")
        if reasons:
            return AgentOutput({"verdict": "VETO", "volume_lots": None, "risk_money": None, "risk_pct": None, "reasons": reasons})
        q, spec, eq = s.quote, s.spec, ctx.account.equity
        entry = q.ask if sig["action"] == "LONG" else q.bid
        tp = live_target(sig, entry)
        lots, rm = sizing.size_position(spec, eq, L.max_risk_per_trade_pct, entry, sig["sl"], q.ask - q.bid)
        pct = rm / eq * 100 if eq else None
        if lots <= 0:
            reasons.append("minimum volume would exceed risk budget")
        stop_dist = abs(entry - sig["sl"])
        if stop_dist <= 0 or (q.ask - q.bid) / stop_dist > L.max_spread_to_stop_ratio:
            reasons.append("spread too large relative to stop")
        if ctx.consecutive_losses >= L.max_consecutive_losses:
            reasons.append(f"{ctx.consecutive_losses} consecutive losses: pause for review")
        if ctx.day_start_equity and (ctx.day_start_equity - eq) / ctx.day_start_equity * 100 >= L.max_daily_drawdown_pct:
            reasons.append("daily drawdown limit reached")
        if len(ctx.open_positions) >= L.max_concurrent_positions:
            reasons.append("max concurrent positions reached")
        rr = abs(tp - entry) / stop_dist if stop_dist > 0 else 0
        if rr < L.min_reward_risk:
            reasons.append(f"RR at live price {rr:.2f} < {L.min_reward_risk}")
        verdict = "VETO" if reasons else "APPROVE"
        return AgentOutput({"verdict": verdict, "volume_lots": lots or None, "risk_money": round(rm, 2) if rm else None,
                            "risk_pct": round(pct, 4) if pct else None,
                            "reasons": reasons or [f"lots={lots}, risk={pct:.3f}% of equity, RR={rr:.2f}"]})

    def respond(self, ctx: CycleContext, obj: dict) -> dict:
        s = ctx.snapshot
        sig = (ctx.proposal or {}).get("signal") or {}
        if obj["category"] == "EXECUTION" and s.quote and sig:
            entry = s.quote.ask if sig["action"] == "LONG" else s.quote.bid
            d = abs(entry - sig["sl"])
            ratio = (s.quote.ask - s.quote.bid) / d if d else 1
            return {"objection_id": obj["id"], "resolved": ratio <= ctx.limits.limits.max_spread_to_stop_ratio,
                    "facts": [f"spread/stop ratio {ratio:.3f} (limit {ctx.limits.limits.max_spread_to_stop_ratio})"]}
        return {"objection_id": obj["id"], "resolved": False, "facts": ["correlated exposure cannot be netted out"]}


# ---------------------------------------------------------------- execution & performance
class ExecutionAgent(BaseAgent):
    name = AgentName.EXECUTION

    def analyze(self, ctx: CycleContext) -> AgentOutput:  # execution itself is done by the orchestrator
        ex = getattr(ctx, "execution_result", None) or {"venue": "NONE", "client_order_id": None, "status": "NOT_EXECUTED",
                                                        "broker_confirmation": None, "reconciliation_ok": None}
        return AgentOutput(ex)


class PerformanceAgent(BaseAgent):
    name = AgentName.PERFORMANCE

    def analyze(self, ctx: CycleContext) -> AgentOutput:
        res = getattr(ctx, "performance_result", None) or {"trade_id": None, "expected_r": None, "actual_r": None,
                                                           "error_categories": [], "lessons": []}
        return AgentOutput(res)


AGENT_CLASSES = {c.name: c for c in (ChiefAgent, MarketStructureAgent, QuantAgent, MacroAgent, RiskManagerAgent,
                                       SkepticAgent, ExecutionAgent, PerformanceAgent)}


def live_target(sig: dict, entry: float) -> float:
    """TP re-anchored to the live entry for R-multiple targets (same rule as the backtester)."""
    rr = sig.get("rr_target")
    if not rr:
        return sig["tp"]
    r = abs(entry - sig["sl"]) * rr
    return entry + r if sig["action"] == "LONG" else entry - r


def now_utc() -> datetime:
    return utcnow()

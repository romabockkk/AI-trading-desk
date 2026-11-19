"""The eight desk agents (TEAM_RULES.md §2).

Each agent is a deterministic, testable function of the shared workspace and the
market snapshot. They post structured AgentReports; later agents read earlier
reports. Nothing here places orders — execution is a separate module behind the
Risk Manager's approval.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from ..analysis.smc import atr, detect_setup
from ..config import Settings
from ..data.market_data import MarketSnapshot
from ..journal import Journal
from ..memory import Memory
from ..models import AgentReport, Decision, Objection, Severity, Stance, TradeProposal, iso
from ..protections import check_protections
from ..sizing import SizeResult, position_size
from ..workspace import Workspace


@dataclass
class AccountState:
    equity: float
    available: float
    verified: bool                       # True = read from OKX demo account, False = assumed
    open_positions: list[dict] = field(default_factory=list)
    pending_orders: list[dict] = field(default_factory=list)
    pos_mode: str = "net_mode"
    source: str = ""


@dataclass
class DeskContext:
    settings: Settings
    snapshot: MarketSnapshot
    account: AccountState
    journal: Journal
    memory: Memory
    news: dict[str, Any] = field(default_factory=dict)       # filled by orchestrator
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def data_ts(self) -> str | None:
        return iso(self.snapshot.last_close_time)


# --------------------------------------------------------------------------- 1
class TechnicalAnalyst:
    name = "Technical Analyst"

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s = ctx.snapshot
        setup = detect_setup(s.m15, s.h1, s.h4, ctx.settings.strategy)
        ws.context["setup"] = setup
        findings = [f"H4 bias: {setup.details.get('h4_bias')} ({(setup.details.get('h4') or {}).get('reason', setup.details.get('reason', ''))})",
                    f"Setup status: {setup.status}"] + setup.notes
        proposal = None
        if setup.status == "READY":
            proposal = TradeProposal(
                inst_id=s.inst_id, direction=setup.direction, entry=round(setup.entry, 8),
                stop_loss=round(setup.stop_loss, 8), take_profit=round(setup.take_profit, 8),
                setup={"strategy_version": ctx.settings.strategy.version, **setup.details},
                reasoning=setup.notes)
            findings.append(f"Proposed {setup.direction}: entry {setup.entry:g}, SL {setup.stop_loss:g}, "
                            f"TP {setup.take_profit:g}, R:R {proposal.rr:.2f}")
            stance, conf, action = Stance.SUPPORT, 0.65, "Send proposal to Quant, News, Strategy, Devil's Advocate and Risk"
        elif setup.status == "FORMING":
            stance, conf, action = Stance.NEUTRAL, 0.4, "Wait for M15 confirmation"
        else:
            stance, conf, action = Stance.NEUTRAL, 0.6, "No valid setup — stay flat"
        return ws.post(AgentReport(self.name, s.inst_id, stance, conf, findings, evidence=setup.details,
                                   sources=s.sources[:1], data_timestamp=ctx.data_ts,
                                   recommended_action=action, proposal=proposal))


# --------------------------------------------------------------------------- 2
class QuantAnalyst:
    name = "Quant Analyst"

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s = ctx.snapshot
        m15 = s.m15
        findings, objections, ev = [], [], {}
        # Volatility regime: current M15 ATR% vs its own 300-bar distribution
        closes = m15["close"].to_numpy()
        tr = np.maximum(m15["high"].to_numpy()[1:] - m15["low"].to_numpy()[1:],
                        np.abs(m15["high"].to_numpy()[1:] - closes[:-1]))
        tr = np.maximum(tr, np.abs(m15["low"].to_numpy()[1:] - closes[:-1]))
        atr_series = np.convolve(tr, np.ones(14) / 14, mode="valid")
        cur_atr = atr_series[-1]
        pct = float((atr_series < cur_atr).mean() * 100)
        ev["atr_m15"] = float(cur_atr)
        ev["atr_pct_of_price"] = float(cur_atr / closes[-1] * 100)
        ev["atr_percentile"] = pct
        findings.append(f"M15 ATR {cur_atr:g} ({ev['atr_pct_of_price']:.3f}% of price), percentile {pct:.0f}")
        if pct >= 95:
            objections.append(Objection("Volatility in top 5% of recent range — stops likely to be run", Severity.MAJOR,
                                        f"ATR percentile {pct:.0f}"))
        # Volume: last 8 bars vs 96-bar average
        vol = m15["volume"].to_numpy()
        rel_vol = float(vol[-8:].mean() / max(vol[-96:].mean(), 1e-9))
        ev["relative_volume_2h"] = rel_vol
        findings.append(f"Relative volume (last 2h vs 24h): {rel_vol:.2f}x")
        # H1 momentum agreement: EMA50 slope
        h1c = s.h1["close"]
        ema = h1c.ewm(span=50, adjust=False).mean()
        slope = float((ema.iloc[-1] - ema.iloc[-6]) / ema.iloc[-6] * 100)
        ev["h1_ema50_slope_pct_5h"] = slope
        findings.append(f"H1 EMA50 slope over 5h: {slope:+.3f}%")
        # Funding / OI
        if s.funding:
            fr = float(s.funding.get("fundingRate") or 0)
            ev["funding_rate"] = fr
            findings.append(f"Funding rate: {fr * 100:+.4f}% per period")
        if s.open_interest:
            ev["open_interest_usd"] = float(s.open_interest.get("oiUsd") or 0)
            findings.append(f"Open interest: {ev['open_interest_usd'] / 1e6:,.0f}M USD")

        prop = (ws.get(TechnicalAnalyst.name) or AgentReport("", "", Stance.NEUTRAL, 0, [])).proposal
        stance, conf = Stance.NEUTRAL, 0.5
        if prop:
            sign = 1 if prop.direction == "LONG" else -1
            fr = ev.get("funding_rate", 0.0)
            if fr * sign > 0.0005:
                objections.append(Objection(f"Funding {fr * 100:+.3f}% — positioning crowded in trade direction",
                                            Severity.MAJOR, "funding > 0.05%/period"))
            if slope * sign < -0.5:
                objections.append(Objection("H1 EMA50 trending firmly against the trade", Severity.MINOR, f"slope {slope:+.2f}%"))
            if rel_vol < 0.6:
                objections.append(Objection("Displacement on thin volume", Severity.MINOR, f"rel vol {rel_vol:.2f}x"))
            majors = [o for o in objections if o.severity in (Severity.MAJOR, Severity.CRITICAL)]
            stance = Stance.OPPOSE if majors else Stance.SUPPORT
            conf = 0.55 if not objections else 0.45
        return ws.post(AgentReport(self.name, s.inst_id, stance, conf, findings, ev, s.sources, ctx.data_ts,
                                   objections, "Statistics attached for Risk Manager"))


# --------------------------------------------------------------------------- 3
class NewsAnalyst:
    name = "News Analyst"
    KEYWORDS = ("delist", "suspend", "maintenance", "upgrade", "halt", "migration")

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s = ctx.snapshot
        base = s.inst_id.split("-")[0].upper()
        findings, objections, sources = [], [], []
        news = ctx.news
        if news.get("announcements_error"):
            findings.append(f"OKX announcements unavailable: {news['announcements_error']}")
        else:
            sources.append("OKX /api/v5/support/announcements")
            relevant = []
            for a in news.get("announcements", []):
                t = a.get("title", "")
                ts = datetime.fromtimestamp(int(a.get("pTime", 0)) / 1000, tz=timezone.utc)
                if ctx.now - ts > timedelta(days=3):
                    continue
                if base in t.upper() or any(k in t.lower() for k in self.KEYWORDS):
                    relevant.append(f"{ts:%Y-%m-%d %H:%M} — {t}")
            findings.append(f"{len(relevant)} relevant OKX announcement(s) in last 72h")
            findings += relevant[:5]
            if any(base in r.upper() and ("delist" in r.lower() or "suspend" in r.lower()) for r in relevant):
                objections.append(Objection(f"Exchange announcement affecting {base}", Severity.CRITICAL))
        events = news.get("events", [])
        if events is None:
            findings.append("No macro calendar file (config/events.json) — macro risk not checked")
        else:
            sources.append("config/events.json (user-maintained macro calendar)")
            for e in events:
                t = datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
                window = timedelta(minutes=int(e.get("blackout_minutes", 60)))
                if t - window <= ctx.now <= t + window and e.get("impact", "high") == "high":
                    objections.append(Objection(f"High-impact event blackout: {e['title']} at {e['time']}", Severity.CRITICAL))
                elif ctx.now < t <= ctx.now + timedelta(hours=12):
                    findings.append(f"Upcoming: {e['title']} at {e['time']}")
        findings.append("Limitation: no general crypto/macro news feed is connected; sentiment not assessed")
        connected = bool(sources)
        stance = Stance.OPPOSE if objections else (Stance.NEUTRAL if connected else Stance.UNAVAILABLE)
        return ws.post(AgentReport(self.name, s.inst_id, stance, 0.3 if connected else 0.0, findings,
                                   {"base_ccy": base}, sources, iso(ctx.now), objections,
                                   "Connect a news feed to raise coverage" if connected else "News data unavailable"))


# --------------------------------------------------------------------------- 4
class StrategyResearcher:
    name = "Strategy Researcher"

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s = ctx.snapshot
        prop = (ws.get(TechnicalAnalyst.name) or AgentReport("", "", Stance.NEUTRAL, 0, [])).proposal
        findings, objections, ev = [], [], {}
        bt_path = ctx.settings.state_dir / "backtests" / f"{s.inst_id}.json"
        bt = json.loads(bt_path.read_text()) if bt_path.exists() else None
        if bt:
            oos = bt.get("out_of_sample", {})
            ev["backtest"] = {"period": bt.get("period"), "oos": oos, "version": bt.get("strategy_version")}
            findings.append(f"Backtest on record ({bt.get('period')}): OOS {oos.get('trades', 0)} trades, "
                            f"expectancy {oos.get('expectancy_r', 0):+.2f}R, PF {oos.get('profit_factor', 0):.2f}")
            if bt.get("strategy_version") != ctx.settings.strategy.version:
                objections.append(Objection("Backtest was run on another strategy version", Severity.MAJOR))
            if oos.get("trades", 0) >= 20 and oos.get("expectancy_r", 0) <= 0:
                objections.append(Objection("Out-of-sample expectancy is not positive", Severity.MAJOR,
                                            f"{oos.get('expectancy_r'):+.2f}R over {oos.get('trades')} trades"))
            elif oos.get("trades", 0) < 20:
                objections.append(Objection("Too few out-of-sample trades to judge the edge", Severity.MINOR))
            full = bt.get("all", {})
            if full.get("trades", 0) >= 30 and full.get("expectancy_r", 0) <= 0:
                objections.append(Objection("Full backtest sample has non-positive expectancy", Severity.MAJOR,
                                            f"{full.get('expectancy_r'):+.2f}R over {full.get('trades')} trades"))
        else:
            objections.append(Objection("No backtest on record for this instrument — run `backtest` first", Severity.MINOR))
        live = [t for t in ctx.journal.closed_trades() if t.get("inst_id") == s.inst_id]
        r = [t["result_r"] for t in live if t.get("result_r") is not None]
        findings.append(f"Demo trades closed on {s.inst_id}: {len(r)}" + (f", avg {np.mean(r):+.2f}R" if r else ""))
        if prop:
            ok = prop.setup.get("strategy_version") == ctx.settings.strategy.version
            findings.append("Setup follows STRATEGY.md v%s rule set: %s" % (ctx.settings.strategy.version, "yes" if ok else "NO"))
            if not ok:
                objections.append(Objection("Setup does not match the current tested rules", Severity.CRITICAL))
            mem = ctx.memory.recall(s.inst_id, prop.setup.get("zone", {}).get("type"))
            ev["memory"] = mem["setup_stats"]
            st = mem["setup_stats"]
            if st["trades"]:
                findings.append(f"Memory: {st['trades']} past {st['setup_type']} trades, win rate {st['win_rate']:.0%}, avg {st['avg_r']:+.2f}R")
        stance = Stance.NEUTRAL if not prop else (Stance.OPPOSE if any(o.severity in (Severity.MAJOR, Severity.CRITICAL) for o in objections) else Stance.SUPPORT)
        return ws.post(AgentReport(self.name, s.inst_id, stance, 0.5, findings, ev,
                                   [str(bt_path)] if bt else [], ctx.data_ts, objections))


# --------------------------------------------------------------------------- 5
class DevilsAdvocate:
    name = "Devil's Advocate"

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s = ctx.snapshot
        tech = ws.get(TechnicalAnalyst.name)
        prop = tech.proposal if tech else None
        objections: list[Objection] = []
        findings: list[str] = []
        if not prop:
            findings.append("No proposal to challenge — staying out is the default")
            return ws.post(AgentReport(self.name, s.inst_id, Stance.NEUTRAL, 0.6, findings, data_timestamp=ctx.data_ts,
                                       recommended_action="Stay flat"))
        lv = prop.setup.get("levels", {})
        if lv.get("liquidity_obstacles_before_min_rr"):
            objections.append(Objection("Opposing liquidity sits before the 2R target — price may reverse there",
                                        Severity.MAJOR, f"levels {lv['liquidity_obstacles_before_min_rr'][:3]}"))
        a = lv.get("atr_m15") or atr(s.m15)
        sweep = prop.setup.get("sweep", {})
        depth = abs(prop.setup.get("m15", {}).get("sweep_extreme", sweep.get("level", 0)) - sweep.get("level", 0))
        if a and depth < 0.2 * a:
            objections.append(Objection("Sweep barely took the level (shallow wick) — may be noise", Severity.MINOR,
                                        f"depth {depth:g} vs ATR {a:g}"))
        if prop.rr < ctx.settings.risk.min_rr + 0.25:
            objections.append(Objection("R:R only marginally above minimum; fees erode it", Severity.MINOR, f"R:R {prop.rr:.2f}"))
        dist = abs(s.price - prop.entry) / s.price * 100
        if dist > 1.0:
            objections.append(Objection("Entry is far from current price; fill uncertain", Severity.MINOR, f"{dist:.2f}% away"))
        # read and amplify other agents' concerns
        for r in ws.reports:
            if r.agent in (self.name, TechnicalAnalyst.name):
                continue
            for o in r.objections:
                objections.append(Objection(f"[{r.agent}] {o.text}", o.severity, o.evidence))
            if r.stance == Stance.UNAVAILABLE:
                objections.append(Objection(f"{r.agent} has no data — part of the picture is missing", Severity.MINOR))
        mem = ctx.memory.recall(s.inst_id, None, k=3)["recent_same_instrument"]
        if len(mem) >= 2 and all((l.get("result_r") or 0) < 0 for l in mem[-2:]):
            objections.append(Objection("Last two trades on this instrument lost", Severity.MINOR))
        contra = ws.contradictions()
        findings += [f"Contradiction: {c}" for c in contra] or ["No direct contradictions between analysts"]
        findings.append(f"{len(objections)} objection(s) raised")
        serious = any(o.severity in (Severity.MAJOR, Severity.CRITICAL) for o in objections)
        return ws.post(AgentReport(self.name, s.inst_id, Stance.OPPOSE if serious else Stance.NEUTRAL,
                                   0.6, findings, data_timestamp=ctx.data_ts, objections=objections,
                                   recommended_action="Reject" if serious else "Proceed only if Risk approves"))


# --------------------------------------------------------------------------- 6
class RiskManager:
    """Independent veto authority. Sizing: size = risk money / stop distance, fees included."""
    name = "Risk Manager"

    def size(self, prop: TradeProposal, ctx: DeskContext) -> SizeResult:
        inst, c = ctx.snapshot.instrument, ctx.settings.costs
        lot = float(inst.get("lotSz", 1))
        return position_size(prop.entry, prop.stop_loss, ctx.account.equity, ctx.account.available,
                             ctx.settings.risk.risk_per_trade_pct, ctx.settings.leverage,
                             float(inst.get("ctVal", 1)), lot, float(inst.get("minSz", lot)),
                             c.taker_fee, c.slippage)

    def run(self, ws: Workspace, ctx: DeskContext) -> AgentReport:
        s, lim = ctx.snapshot, ctx.settings.risk
        tech = ws.get(TechnicalAnalyst.name)
        prop = tech.proposal if tech else None
        findings, objections, ev = [], [], {}
        prot = check_protections(ctx.journal.closed_trades(), ctx.now, lim,
                                 ctx.settings.state_dir / "KILL")
        findings += prot.reasons or ["Protections clear (StoplossGuard, MaxDrawdown, Cooldown, kill switch)"]
        for r in prot.reasons:
            objections.append(Objection(r, Severity.CRITICAL))
        findings.append(f"Equity {ctx.account.equity:,.2f} USDT ({'verified from OKX demo' if ctx.account.verified else 'ASSUMED — not verified'})")
        n_open = len(ctx.account.open_positions) + len(ctx.account.pending_orders)
        if n_open >= lim.max_open_positions:
            objections.append(Objection(f"{n_open} open position/order(s) — limit {lim.max_open_positions}", Severity.CRITICAL))
        if s.problems:
            objections.append(Objection("Market data problems: " + "; ".join(s.problems), Severity.CRITICAL))
        if prop:
            sign = 1 if prop.direction == "LONG" else -1
            if (prop.entry - prop.stop_loss) * sign <= 0:
                objections.append(Objection("Stop loss on the wrong side / missing", Severity.CRITICAL))
            if prop.rr < lim.min_rr:
                objections.append(Objection(f"R:R {prop.rr:.2f} below minimum {lim.min_rr}", Severity.CRITICAL))
            sz = self.size(prop, ctx)
            if not sz.ok:
                objections.append(Objection(f"Cannot size the position: {sz.reason}", Severity.CRITICAL))
            else:
                ev.update(contracts=sz.contracts, risk_usd=sz.risk_usd, notional=sz.notional, margin=sz.margin,
                          leverage=ctx.settings.leverage, capped_by_margin=sz.capped_by_margin)
                findings.append(f"Size {sz.contracts} contracts = {sz.notional:,.2f} USDT notional, margin {sz.margin:,.2f} "
                                f"at {ctx.settings.leverage}x, max loss incl. fees {sz.risk_usd:.2f} USDT "
                                f"({sz.risk_usd / ctx.account.equity * 100:.2f}% of equity)"
                                + (" — reduced to fit available margin" if sz.capped_by_margin else ""))
                liq_dist = prop.entry / ctx.settings.leverage * 0.9
                if prop.risk_per_unit > 0.5 * liq_dist:
                    objections.append(Objection("Stop too close to estimated liquidation distance", Severity.CRITICAL))
                if sz.risk_usd > ctx.account.equity * lim.risk_per_trade_pct / 100 + 1e-6:
                    objections.append(Objection("Planned risk exceeds limit", Severity.CRITICAL))
                contracts, risk_usd = sz.contracts, sz.risk_usd
                prop.position_size_contracts, prop.risk_usd = contracts, risk_usd
        critical = [o for o in objections if o.severity == Severity.CRITICAL]
        if prop and not critical:
            stance, action = Stance.SUPPORT, "Risk approved for DEMO"
        elif prop or prot.blocked or critical:
            stance, action = Stance.VETO, "VETO — " + "; ".join(o.text for o in critical[:3])
        else:
            stance, action = Stance.NEUTRAL, "No trade to evaluate"
        return ws.post(AgentReport(self.name, s.inst_id, stance, 0.9, findings, ev,
                                   [ctx.account.source] if ctx.account.source else [], ctx.data_ts,
                                   objections, action))


# --------------------------------------------------------------------------- 7
class HeadTrader:
    name = "Head Trader"

    def decide(self, ws: Workspace, ctx: DeskContext) -> tuple[Decision, str]:
        tech = ws.get(TechnicalAnalyst.name)
        risk = ws.get(RiskManager.name)
        da = ws.get(DevilsAdvocate.name)
        news = ws.get(NewsAnalyst.name)
        quant = ws.get(QuantAnalyst.name)
        if ctx.snapshot.problems:
            return Decision.NOT_ENOUGH_DATA, "Market data failed validation: " + "; ".join(ctx.snapshot.problems)
        if ctx.settings.require_news and news and news.stance == Stance.UNAVAILABLE:
            return Decision.NOT_ENOUGH_DATA, "News data required but unavailable"
        if not tech or not tech.proposal:
            status = ws.context.get("setup").status if ws.context.get("setup") else "NONE"
            if status == "FORMING":
                return Decision.WAIT_FOR_CONFIRMATION, "Setup forming — waiting for M15 confirmation. NO TRADE now."
            return Decision.REJECTED, f"No valid setup (status {status}). NO TRADE."
        if risk and risk.stance == Stance.VETO:
            return Decision.REJECTED, "Risk Manager veto (cannot be overridden): " + risk.recommended_action
        all_obj = da.objections if da else []
        critical = [o for o in all_obj if o.severity == Severity.CRITICAL]
        major = [o for o in all_obj if o.severity == Severity.MAJOR]
        if critical:
            return Decision.REJECTED, "Critical objection unresolved: " + critical[0].text
        if len(major) >= 2 or (major and quant and quant.stance == Stance.OPPOSE):
            return Decision.REJECTED, "Unresolved major objections: " + "; ".join(o.text for o in major)
        why = [f"{tech.proposal.direction} setup meets v{ctx.settings.strategy.version} rules",
               f"R:R {tech.proposal.rr:.2f}", "Risk Manager approved"]
        if major:
            why.append(f"accepted 1 major objection with explanation: {major[0].text} — mitigated by fixed 1% risk and hard SL")
        if not ctx.account.verified:
            why.append("equity is ASSUMED; execution needs a verified OKX demo account")
        return Decision.APPROVED_FOR_DEMO, "; ".join(why)

    def run(self, ws: Workspace, ctx: DeskContext) -> tuple[AgentReport, Decision, str]:
        decision, why = self.decide(ws, ctx)
        tech = ws.get(TechnicalAnalyst.name)
        findings = [f"Decision: {decision.value}", why]
        findings += [f"Read: {r.agent} → {r.stance.value}" for r in ws.reports]
        rep = ws.post(AgentReport(self.name, ctx.snapshot.inst_id, Stance.SUPPORT if decision == Decision.APPROVED_FOR_DEMO else Stance.NEUTRAL,
                                  0.6, findings, data_timestamp=ctx.data_ts,
                                  recommended_action="Hand to execution (demo)" if decision == Decision.APPROVED_FOR_DEMO else "Stay flat",
                                  proposal=tech.proposal if tech and decision == Decision.APPROVED_FOR_DEMO else None))
        return rep, decision, why


# --------------------------------------------------------------------------- 8
class TradeAuditor:
    name = "Trade Auditor"

    def record_cycle(self, ws: Workspace, ctx: DeskContext, decision: Decision, why: str) -> AgentReport:
        tech = ws.get(TechnicalAnalyst.name)
        entry = ctx.journal.append("decision", {
            "cycle_id": ws.cycle_id, "inst_id": ctx.snapshot.inst_id, "decision": decision.value, "why": why,
            "proposal": tech.proposal.to_dict() if tech and tech.proposal else None,
            "opinions": {r.agent: r.stance.value for r in ws.reports},
            "vetoes": [o.text for r in ws.reports if r.stance == Stance.VETO for o in r.objections],
            "data_timestamp": ctx.data_ts, "sources": ctx.snapshot.sources,
        })
        ok, msg = ctx.journal.verify()
        return ws.post(AgentReport(self.name, ctx.snapshot.inst_id, Stance.NEUTRAL, 1.0,
                                   [f"Decision recorded (hash {entry['hash'][:12]}…)", f"Journal check: {msg}"],
                                   data_timestamp=ctx.data_ts,
                                   objections=[] if ok else [Objection("Journal integrity failure", Severity.CRITICAL)]))

    def review_closed(self, trade: dict, memory: Memory) -> dict:
        """Post-trade review (TRADE_JOURNAL.md §5). Returns a lesson and stores it."""
        planned = trade.get("risk_usd") or 0
        pnl = trade.get("pnl_usd") or 0
        r = pnl / planned if planned else None
        issues = []
        if planned and -pnl > planned * 1.25:
            issues.append("actual loss exceeded planned risk by >25% (slippage/gap)")
        if not trade.get("sl_confirmed", True):
            issues.append("stop loss was not confirmed on exchange")
        lesson = {"inst_id": trade.get("inst_id"), "trade_id": trade.get("trade_id"),
                  "setup_type": (trade.get("setup") or {}).get("zone", {}).get("type"),
                  "direction": trade.get("direction"), "result_r": r, "pnl_usd": pnl,
                  "rule_violations": issues,
                  "note": ("loss" if pnl < 0 else "win") + (": " + "; ".join(issues) if issues else ", rules followed")}
        memory.add_lesson(lesson)
        return lesson


TEAM_ORDER = [TechnicalAnalyst, QuantAnalyst, NewsAnalyst, StrategyResearcher, DevilsAdvocate, RiskManager]

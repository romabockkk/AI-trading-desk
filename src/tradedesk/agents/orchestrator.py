"""One analysis cycle:

 1 data snapshot + validation         5 Skeptic objections, agents answer with facts (<= 2 rounds)
 2 Market Structure, Quant, Macro     6 Risk Manager independent check (VETO)
   run independently                  7 deterministic Risk Gate -> ALLOW / DENY
 3 (paper positions are marked first) 8 Chief records the decision with ids of its inputs
 4 Chief hypothesis + alternative     9 Execution only on ALLOW, only in the internal PaperBroker
                                     10 Performance analyses closed trades

Every report and debate message is persisted. A failed critical agent => NO_TRADE.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from ..config import LoadedRiskLimits, Mode, Settings
from ..data.service import MarketService, persist_snapshot
from ..db import database as db
from ..execution.paper import PaperBroker, PaperError
from ..learning import experiments, registry, review
from ..risk import gate as rgate
from ..schemas.agents import AgentName, AgentReport, ReportStatus, critical_failure
from ..schemas.market import Timeframe
from ..schemas.trading import Action, TradeProposal
from ..strategies.library import ALL_STRATEGIES
from .base import load_agent_configs
from .llm import LLMClient
from .team import AGENT_CLASSES, SEVERITY_ORDER, ActiveStrategy, CycleContext, live_target

RESPONDER = {"STRUCTURE": AgentName.MARKET_STRUCTURE, "STATISTICS": AgentName.QUANT, "OVERFITTING": AgentName.QUANT,
             "NEWS": AgentName.MACRO, "DATA": AgentName.CHIEF, "CONTRADICTION": AgentName.CHIEF,
             "EXECUTION": AgentName.RISK_MANAGER, "CORRELATION": AgentName.RISK_MANAGER, "OTHER": AgentName.CHIEF}


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, limits: LoadedRiskLimits,
                 data: MarketService, llm: LLMClient | None = None, agent_config_path: str = "config/agents.toml"):
        self.conn, self.settings, self.limits, self.data = conn, settings, limits, data
        cfgs = load_agent_configs(agent_config_path)
        self.agents = {n: AGENT_CLASSES[n](cfgs[n], llm) for n in AgentName}
        self.paper = PaperBroker(conn, initial_balance=settings.tradedesk_paper_initial_balance,
                                 slippage_points=settings.tradedesk_paper_slippage_points,
                                 stop_out_level_pct=settings.tradedesk_paper_stop_out_level_pct)
        registry.ensure_registered(conn)
        from ..marketdata import specs as _specs
        _specs.seed(conn)

    def instruments_and_quotes(self, now: datetime, symbols: set[str]) -> tuple[dict, dict]:
        quotes, insts = {}, {}
        for s in symbols:
            lt = self.data.latest(self.conn, s)
            if lt:
                quotes[s] = lt["quote"]
            inst, _ = self.data.instrument(self.conn, s, now)
            if inst:
                insts[s] = inst
        return quotes, insts

    def monitor_positions(self, now: datetime) -> dict:
        """Manage open paper positions: fresh quotes -> SL/TP, bars between polls, swaps, stop-out."""
        syms = {p["symbol"] for p in self.paper.open_positions()}
        closed: list[dict] = []
        stale = []
        if syms:
            self.data.poll_quotes(self.conn, now, tuple(syms | {"USDJPY"}))
            quotes, insts = self.instruments_and_quotes(now, syms)
            for s in syms:
                inst, q = insts.get(s), quotes.get(s)
                if inst is None:
                    db.log_error(self.conn, "monitor", "ERROR", f"{s}: open position but instrument spec unavailable")
                    continue
                bars, _ = self.data.bars(self.conn, s, Timeframe.M5, now, 50)
                closed += self.paper.on_bars(s, bars, inst)
                if q is not None:
                    if (now - q.time_utc).total_seconds() > self.limits.limits.max_quote_age_seconds:
                        stale.append(s)
                    closed += self.paper.on_quote(s, q, inst, now, self.limits.limits.max_quote_age_seconds)
            self.paper.apply_swaps(insts, now)
            closed += self.paper.check_stop_out(quotes, insts, now)
        snap = self.paper.snapshot(*self.instruments_and_quotes(now, syms), now) if syms else self.paper.snapshot({}, {}, now)
        dd = (snap["initial_balance"] - snap["equity"]) / snap["initial_balance"] * 100
        if dd >= self.limits.limits.max_total_drawdown_pct and db.get_state(self.conn, "kill_switch") != "ON":
            db.set_state(self.conn, "kill_switch", "ON", "SYSTEM")
            self.conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, details) VALUES (?,?,?,?)",
                              (db.utc_now_iso(), "MAX_DRAWDOWN_CIRCUIT_BREAKER", "CRITICAL",
                               json.dumps({"drawdown_pct": round(dd, 3), "equity": snap["equity"]})))
        lessons = review.analyze_new_trades(self.conn) if closed else []
        return {"closed": closed, "stale_quotes": stale, "lessons": lessons, "reconcile": self.paper.reconcile()}

    # ------------------------------------------------------------------ helpers
    def _strategies(self) -> tuple[list[ActiveStrategy], list[ActiveStrategy]]:
        active, research = [], []
        for r in registry.list_versions(self.conn):
            if r["status"] not in ("ACTIVE_PAPER", "RESEARCH"):
                continue
            cls = ALL_STRATEGIES.get(r["strategy_id"])
            if cls is None:
                continue
            eid, ev = experiments.latest_evidence(self.conn, r["strategy_id"], r["version"])
            item = ActiveStrategy(cls(**json.loads(r["params"])), r["version"], ev, eid)
            (active if r["status"] == "ACTIVE_PAPER" else research).append(item)
        return active, research

    def _save_report(self, r: AgentReport) -> None:
        self.conn.execute(
            "INSERT INTO agent_reports(report_id, cycle_id, agent, created_utc, status, is_critical, snapshot_ids, payload,"
            " llm_provider, llm_model, tokens_in, tokens_out, latency_ms, attempts, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.report_id, r.cycle_id, r.agent.value, r.created_utc.isoformat(), r.status.value, int(r.is_critical),
             json.dumps(r.snapshot_ids), r.model_dump_json(), (r.llm_review or {}).get("provider"),
             (r.llm_review or {}).get("model"), (r.llm_review or {}).get("tokens_in"),
             (r.llm_review or {}).get("tokens_out"), r.latency_ms, r.attempts, r.error))
        if r.status is not ReportStatus.OK:
            db.log_error(self.conn, f"agent:{r.agent.value}", "ERROR" if r.is_critical else "WARN",
                         f"{r.agent.value} {r.status.value}: {r.error}", cycle_id=r.cycle_id)

    def _msg(self, ctx: CycleContext, cycle_id: str, rnd: int, frm: AgentName, to: AgentName | None, mtype: str,
             payload: dict, resolved: bool | None = None) -> dict:
        seq = len(ctx.debate) + 1
        m = {"round": rnd, "seq": seq, "from": frm.value, "to": to.value if to else None, "message_type": mtype,
             "payload": payload, "resolved": resolved}
        self.conn.execute("INSERT INTO debates(cycle_id, round, seq, from_agent, to_agent, message_type, created_utc, payload)"
                          " VALUES (?,?,?,?,?,?,?,?)", (cycle_id, rnd, seq, frm.value, m["to"], mtype, db.utc_now_iso(),
                                                        json.dumps({**payload, "resolved": resolved}, default=str)))
        ctx.debate.append(m)
        return m

    def _run(self, ctx: CycleContext, name: AgentName, cycle_id: str) -> AgentReport:
        r = self.agents[name].run(ctx, cycle_id)
        ctx.reports[name] = r
        self._save_report(r)
        return r

    # ------------------------------------------------------------------ cycle
    def run_cycle(self, symbol: str, now: datetime) -> dict:
        cycle_id = f"cyc-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        db.audit(self.conn, "CHIEF", "CYCLE_STARTED", entity="cycle", entity_id=cycle_id,
                 details={"symbol": symbol, "mode": self.settings.mode.value})
        L = self.limits.limits

        snap = self.data.snapshot(self.conn, symbol, now, L)
        persist_snapshot(self.conn, snap)
        # 3. mark paper positions to the latest bars before deciding
        closed_now = self.monitor_positions(now)["closed"]
        quotes, insts = self.instruments_and_quotes(now, {p["symbol"] for p in self.paper.open_positions()} | {symbol})
        acct_snap = self.paper.snapshot(quotes, insts, now)
        account = self.paper.account_info(quotes, insts, now)
        open_pos = self.paper.open_positions()
        active, research = self._strategies()
        ctx = CycleContext(conn=self.conn, settings=self.settings, limits=self.limits, snapshot=snap, now=now,
                           active=active, research=research, account=account, open_positions=open_pos,
                           consecutive_losses=review.consecutive_losses(self.conn, account_id=self.paper.account_id),
                           day_start_equity=acct_snap["day_start_equity"])

        # 2. independent analysts
        for a in (AgentName.MARKET_STRUCTURE, AgentName.QUANT, AgentName.MACRO):
            self._run(ctx, a, cycle_id)
        # 4. chief hypothesis + alternative
        chief = self.agents[AgentName.CHIEF]
        try:
            ctx.proposal = chief.hypothesis(ctx)
        except Exception as e:  # noqa: BLE001
            ctx.proposal = {"hypothesis": "NO_TRADE: chief failed", "alternative": "-", "proposed_action": "NO_TRADE",
                            "signal": None, "reason": f"chief hypothesis error {e}"}
        self._msg(ctx, cycle_id, 1, AgentName.CHIEF, None, "HYPOTHESIS",
                  {"text": ctx.proposal["hypothesis"], "action": ctx.proposal["proposed_action"],
                   "signal": ctx.proposal.get("signal")})
        self._msg(ctx, cycle_id, 1, AgentName.CHIEF, None, "ALTERNATIVE", {"text": ctx.proposal["alternative"]})

        # 5. debate (max 2 rounds), only when a trade is on the table
        sk = self._run(ctx, AgentName.SKEPTIC, cycle_id)
        objections = (sk.payload or {}).get("objections", []) if sk.status is ReportStatus.OK else []
        if sk.llm_review and sk.llm_review.get("assessment") == "DISAGREE":
            for k, c in enumerate(sk.llm_review.get("concerns", [])[:3], start=1):
                objections.append({"id": f"LLM-{k}", "category": "OTHER", "text": f"(LLM) {c}", "severity": "MEDIUM"})
        if ctx.proposal["proposed_action"] != "NO_TRADE":
            for rnd in (1, 2):
                pending = [o for o in objections if not o.get("_resolved")]
                if rnd == 2:
                    pending = [o for o in pending if SEVERITY_ORDER[o["severity"]] >= 2]
                if not pending:
                    break
                for o in pending:
                    to = RESPONDER.get(o["category"], AgentName.CHIEF)
                    m = self._msg(ctx, cycle_id, rnd, AgentName.SKEPTIC, to, "OBJECTION", o)
                    resp = self.agents[to].respond(ctx, o) if hasattr(self.agents[to], "respond") else \
                        {"objection_id": o["id"], "resolved": False, "facts": ["agent cannot respond"]}
                    self._msg(ctx, cycle_id, rnd, to, AgentName.SKEPTIC, "RESPONSE", resp, resolved=resp["resolved"])
                    o["_resolved"] = resp["resolved"]
                    m["resolved"] = resp["resolved"]
            unresolved_crit = [o for o in objections if not o.get("_resolved") and o["severity"] == "CRITICAL"]
            if unresolved_crit:
                self._msg(ctx, cycle_id, 2, AgentName.SKEPTIC, AgentName.CHIEF, "DEMAND_NO_TRADE",
                          {"objection_ids": [o["id"] for o in unresolved_crit]})
            elif not [o for o in objections if not o.get("_resolved")]:
                self._msg(ctx, cycle_id, 2, AgentName.SKEPTIC, AgentName.CHIEF, "CONCESSION", {"text": "all objections answered"})
        elif sk.status is ReportStatus.OK:
            self._msg(ctx, cycle_id, 1, AgentName.SKEPTIC, AgentName.CHIEF, "CONCESSION",
                      {"text": "no trade proposed; objections recorded only", "objection_count": len(objections)})

        # 6. risk manager
        rm = self._run(ctx, AgentName.RISK_MANAGER, cycle_id)
        # chief final
        cr = self._run(ctx, AgentName.CHIEF, cycle_id)
        crit = critical_failure(list(ctx.reports.values()))
        final = (cr.payload or {}).get("final_action", "NO_TRADE") if cr.status is ReportStatus.OK else "NO_TRADE"
        llm_chief = cr.llm_review or {}
        reasons = list((cr.payload or {}).get("reasons", []))
        if llm_chief.get("assessment") == "DISAGREE" and final != "NO_TRADE":
            final = "NO_TRADE"
            reasons.append("LLM review of Chief disagreed (LLM may only downgrade)")
        if crit:
            final = "NO_TRADE"
            reasons.append(f"critical agent failure: {[c.value for c in crit]}")

        # 7. deterministic risk gate
        sig = ctx.proposal.get("signal")
        decision_id = f"dec-{uuid.uuid4().hex[:12]}"
        gate_res, gate_reasons, lots, risk_money = "NOT_EVALUATED", [], None, None
        if final in ("LONG", "SHORT") and sig and snap.quote:
            sig = dict(sig, tp=live_target(sig, snap.quote.ask if final == "LONG" else snap.quote.bid))
            proposal = TradeProposal(decision_id=decision_id, cycle_id=cycle_id, symbol=symbol, action=Action(final),
                                     entry=sig["entry"], stop_loss=sig["sl"], take_profit=sig["tp"],
                                     strategy_id=sig["strategy_id"], strategy_version=sig["version"],
                                     snapshot_ids=(snap.snapshot_id,), created_utc=now)
            ordered = frozenset(r["decision_id"] for r in self.conn.execute("SELECT decision_id FROM orders"))
            gi = rgate.GateInput(
                mode=self.settings.mode, proposal=proposal, instrument=snap.instrument, spec_missing=tuple(snap.spec_missing),
                quote=snap.quote, account=acct_snap, now_utc=now, consecutive_losses=ctx.consecutive_losses,
                open_positions=tuple(rgate.OpenPositionRisk(p["symbol"], p["side"], p["planned_risk_money"] or 0.0,
                                                            p["client_order_id"]) for p in open_pos),
                kill_switch_on=db.get_state(self.conn, "kill_switch") == "ON",
                paused=db.get_state(self.conn, "paused") == "true", market_open=snap.market_open, data_valid=snap.valid,
                critical_failures=tuple(c.value for c in crit),
                risk_manager_approved=rm.status is ReportStatus.OK and rm.payload["verdict"] == "APPROVE",
                skeptic_demands_no_trade=any(m["message_type"] == "DEMAND_NO_TRADE" for m in ctx.debate),
                news_blackout=(ctx.payload(AgentName.MACRO) or {}).get("blackout_active"),
                atr=snap.atr(), already_ordered_decision_ids=ordered)
            g = rgate.evaluate(gi, self.limits)
            gate_res = g.result.value
            gate_reasons = [f"{c.name}: {c.detail}" for c in g.failed]
            lots, risk_money = g.volume_lots, g.risk_money
            if gate_res == "DENY":
                reasons.append("Risk Gate DENY")
                self.conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, decision_id, details) VALUES (?,?,?,?,?)",
                                  (db.utc_now_iso(), "GATE_DENY", "INFO", decision_id, json.dumps({"failed": gate_reasons})))
        action = final if (final in ("LONG", "SHORT") and gate_res == "ALLOW") else "NO_TRADE"
        no_trade_reason = None
        if action == "NO_TRADE":
            if gate_res == "DENY":
                no_trade_reason = f"{final} proposed, Risk Gate DENY: " + "; ".join(gate_reasons[:6])
            else:
                no_trade_reason = "; ".join(reasons[-3:]) or ctx.proposal.get("reason")

        # 8. record decision
        objection_rows = [m["payload"] | {"resolved": m.get("resolved")} for m in ctx.debate if m["message_type"] == "OBJECTION"]
        self.conn.execute(
            "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, strategy_id, strategy_version, action,"
            " entry, stop_loss, take_profit, volume_lots, planned_risk_pct, planned_rr, risk_gate_result, risk_gate_reasons,"
            " risk_limits_sha256, reasons, objections, snapshot_ids, report_ids, no_trade_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, cycle_id, now.isoformat(), self.settings.mode.value, symbol,
             sig["strategy_id"] if sig else None, sig["version"] if sig else None,
             action, sig["entry"] if sig else None, sig["sl"] if sig else None, sig["tp"] if sig else None, lots,
             round(risk_money / acct_snap["equity"] * 100, 4) if risk_money else None,
             sig["planned_rr"] if sig else None, gate_res, json.dumps(gate_reasons), self.limits.sha256,
             json.dumps(reasons), json.dumps(objection_rows, default=str), json.dumps([snap.snapshot_id]),
             json.dumps([r.report_id for r in ctx.reports.values()]), no_trade_reason))
        db.audit(self.conn, "CHIEF", "DECISION", entity="decisions", entity_id=decision_id,
                 details={"action": action, "gate": gate_res})

        # 9. execution
        execution = {"venue": "NONE", "client_order_id": None, "status": "NOT_EXECUTED", "broker_confirmation": None,
                     "reconciliation_ok": None}
        if gate_res == "ALLOW" and action in ("LONG", "SHORT"):
            try:
                res = self.paper.place_order(decision_id=decision_id, symbol=symbol, side="BUY" if action == "LONG" else "SELL",
                                             volume_lots=lots, stop_loss=sig["sl"], take_profit=sig["tp"], quote=snap.quote,
                                             inst=snap.instrument, now=now,
                                             max_quote_age_seconds=L.max_quote_age_seconds)
                rec = self.paper.reconcile()
                execution = {"venue": "PAPER", "client_order_id": res.get("client_order_id"),
                             "status": "DUPLICATE_BLOCKED" if res["duplicate"] else "FILLED",
                             "broker_confirmation": {"internal_paper_broker": True, "price": res.get("price"),
                                                     "commission": res.get("commission"), "margin": res.get("margin")},
                             "reconciliation_ok": rec["ok"]}
            except PaperError as e:
                db.log_error(self.conn, "execution", "ERROR", f"paper order rejected: {e}", cycle_id=cycle_id)
                execution = {"venue": "PAPER", "client_order_id": None, "status": "REJECTED",
                             "broker_confirmation": {"error": str(e)}, "reconciliation_ok": self.paper.reconcile()["ok"]}
        ctx.execution_result = execution
        self._run(ctx, AgentName.EXECUTION, cycle_id)

        # 10. performance
        perf = review.analyze_new_trades(self.conn)
        ctx.performance_result = perf[-1] if perf else None
        self._run(ctx, AgentName.PERFORMANCE, cycle_id)

        return {"cycle_id": cycle_id, "decision_id": decision_id, "symbol": symbol, "mode": self.settings.mode.value,
                "data_source": snap.source, "data_is_mock": snap.is_mock, "data_valid": snap.valid,
                "quote_latency_s": snap.quote_latency_s, "spec_missing": snap.spec_missing, "action": action, "risk_gate": gate_res,
                "gate_failures": gate_reasons, "reasons": reasons, "no_trade_reason": no_trade_reason,
                "execution": execution, "closed_trades": closed_now, "lessons": perf,
                "agent_status": {a.value: r.status.value for a, r in ctx.reports.items()},
                "debate_messages": len(ctx.debate)}

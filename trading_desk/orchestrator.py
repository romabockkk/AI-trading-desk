"""Research-cycle graph (AGENT_WORKFLOW.md §2):

    data check → Technical → Quant → News → Strategy → Devil's Advocate
               → [optional Claude reviewer] → Risk Manager (veto) → Head Trader
               → Trade Auditor (journal) → [approval gate] → OKX demo execution
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .agents.llm import ClaudeReviewer
from .agents.team import (TEAM_ORDER, AccountState, DeskContext, DevilsAdvocate, HeadTrader, RiskManager,
                          TradeAuditor)
from .config import Settings
from .data.market_data import load_snapshot
from .data.okx_client import OKXClient, OKXError
from .execution.okx_executor import OKXDemoExecutor
from .journal import Journal
from .memory import Memory
from .models import CycleResult, Decision
from .workspace import Workspace


class Desk:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = OKXClient(settings.okx_base_url, settings.okx_api_key, settings.okx_secret,
                                settings.okx_passphrase)
        self.journal = Journal(settings.state_dir / "journal.jsonl")
        self.memory = Memory(settings.state_dir / "memory.jsonl")
        self.executor = OKXDemoExecutor(self.client, settings, self.journal)
        self.auditor = TradeAuditor()

    # ---------------------------------------------------------------- inputs
    def account_state(self) -> AccountState:
        if not self.s.has_demo_credentials:
            eq = float(os.environ.get("DESK_PAPER_EQUITY", "10000"))
            open_j = self.journal.open_trades()
            return AccountState(eq, eq, False, open_positions=open_j,
                                source=f"assumed paper equity {eq:g} USDT (no OKX demo credentials)")
        self.client.sync_clock()
        cfg = self.client.account_config()
        bal = self.client.balance("USDT")
        det = next((d for d in bal.get("details", []) if d.get("ccy") == "USDT"), {})
        eq = float(det.get("eq") or bal.get("totalEq") or 0)
        avail = float(det.get("availEq") or det.get("availBal") or 0)
        pos = [p for p in self.client.positions() if float(p.get("pos") or 0) != 0]
        pend = self.client.pending_orders()
        return AccountState(eq, avail, True, pos, pend, cfg.get("posMode", "net_mode"),
                            "OKX DEMO /account/balance + /account/positions + /trade/orders-pending")

    def news_inputs(self) -> dict:
        out: dict = {}
        try:
            out["announcements"] = self.client.announcements()
        except OKXError as exc:
            out["announcements_error"] = str(exc)
        ev = Path("config/events.json")
        out["events"] = json.loads(ev.read_text()) if ev.exists() else None
        return out

    # ----------------------------------------------------------------- cycle
    def reconcile(self) -> list[dict]:
        if not self.s.has_demo_credentials:
            return []
        events = self.executor.monitor()
        for e in events:
            if e.get("event") == "closed":
                trade = self.journal.trades().get(e["trade_id"], {})
                lesson = self.auditor.review_closed(trade, self.memory)
                self.journal.append("trade_review", {"trade_ref": e["trade_id"], **lesson})
        return events

    def cycle(self, inst_id: str, execute: bool = False, approve=None) -> CycleResult:
        now = datetime.now(timezone.utc)
        cycle_id = now.strftime("%Y%m%dT%H%M%SZ")
        ws = Workspace(self.s.workspace_dir, cycle_id, inst_id)
        account = self.account_state()
        snap = load_snapshot(self.client, inst_id, self.s.risk.max_data_age_minutes)
        ctx = DeskContext(self.s, snap, account, self.journal, self.memory, self.news_inputs(), now)

        if not snap.problems:
            for agent_cls in TEAM_ORDER:
                if agent_cls is RiskManager and self.s.llm_enabled:
                    ClaudeReviewer(self.s.llm_model).run(ws)
                    # Devil's Advocate re-reads so LLM objections reach the Head Trader
                    da = ws.get(DevilsAdvocate.name)
                    llm = ws.reports[-1]
                    if da and llm.objections:
                        da.objections += [type(o)(f"[Claude Reviewer] {o.text}", o.severity) for o in llm.objections]
                agent_cls().run(ws, ctx)
        head_rep, decision, why = HeadTrader().run(ws, ctx)
        self.auditor.record_cycle(ws, ctx, decision, why)
        result = CycleResult(cycle_id, inst_id, decision, why, ws.reports, head_rep.proposal)

        if decision == Decision.APPROVED_FOR_DEMO and execute:
            if not account.verified:
                result.execution = {"status": "skipped", "reason": "OKX demo credentials not configured"}
            elif approve is not None and not approve(result):
                result.execution = {"status": "skipped", "reason": "not approved by user"}
                self.journal.append("approval_declined", {"cycle_id": cycle_id, "inst_id": inst_id})
            else:
                result.execution = self.executor.submit(result.proposal, snap.instrument, account.pos_mode,
                                                        account.equity)
        ws.save(result)
        return result

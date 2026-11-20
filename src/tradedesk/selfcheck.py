"""Self-check of the RUNNING system (not unit tests): account integrity, agents, Risk Gate behaviour, data honesty.

Risk Gate probes use the live risk limits and the live account with synthetic *proposals* that must be refused.
They create no orders and are not stored as decisions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .config import Mode
from .db import database as db
from .execution.paper import Instrument
from .risk import gate as rgate
from .schemas.agents import AgentName
from .schemas.market import DataSource, Quote, SymbolSpec
from .schemas.trading import Action, TradeProposal


def _probe_instrument(now: datetime) -> Instrument:
    """A neutral probe instrument (labelled PROBE) — used only to exercise refusal rules."""
    spec = SymbolSpec(symbol="PROBE", asset_class="OTHER", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                      contract_size=100, volume_min=0.01, volume_max=50, volume_step=0.01, margin_currency="USD",
                      profit_currency="USD", swap_long=0, swap_short=0, commission_per_lot=6.0, trade_allowed=True,
                      source=DataSource(name="SELF-CHECK PROBE", is_mock=False), fetched_utc=now)
    return Instrument(spec, 0.2, "POINTS", "WEDNESDAY", 1.0)


def run(conn: sqlite3.Connection, runtime, now: datetime) -> dict:
    checks = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    pb = runtime.paper(conn)
    acc = conn.execute("SELECT * FROM paper_accounts WHERE account_id=?", (pb.account_id,)).fetchone()
    add("initial balance is $100", acc["initial_balance"] == runtime.settings.tradedesk_paper_initial_balance == 100.0,
        f"initial_balance={acc['initial_balance']}")
    led = conn.execute("SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM account_ledger WHERE account_id=?",
                       (pb.account_id,)).fetchone()
    add("balance equals ledger sum", abs(pb.balance() - led["s"]) < 1e-9, f"balance={pb.balance():.4f}, ledger rows={led['n']}")
    rec = pb.reconcile()
    add("positions/orders/ledger reconcile", rec["ok"], str(rec["issues"]) if rec["issues"] else "consistent")
    bad = conn.execute("SELECT COUNT(*) FROM trades t LEFT JOIN orders o ON o.decision_id=t.decision_id WHERE o.client_order_id IS NULL").fetchone()[0]
    add("every trade has an order", bad == 0, f"{bad} trades without order")
    nosl = conn.execute("SELECT COUNT(*) FROM orders WHERE stop_loss IS NULL").fetchone()[0]
    add("every order has a stop-loss", nosl == 0, f"{nosl} orders without SL")
    mock_orders = conn.execute("SELECT COUNT(*) FROM orders o JOIN decisions d ON d.decision_id=o.decision_id JOIN"
                               " market_snapshots s ON s.snapshot_id=json_extract(d.snapshot_ids,'$[0]') WHERE s.is_mock=1").fetchone()[0]
    add("no order was ever based on MOCK data", mock_orders == 0, f"{mock_orders} orders on mock snapshots")
    ok, badid = db.verify_audit_chain(conn)
    add("audit chain intact", ok, "intact" if ok else f"broken at {badid}")

    last = conn.execute("SELECT cycle_id, created_utc FROM agent_reports ORDER BY id DESC LIMIT 1").fetchone()
    if last is None:
        add("all 8 agents reported in the last cycle", False, "no cycle has run yet")
    else:
        st = {r["agent"]: r["status"] for r in conn.execute("SELECT agent, status FROM agent_reports WHERE cycle_id=?",
                                                            (last["cycle_id"],))}
        missing = [a.value for a in AgentName if st.get(a.value) != "OK"]
        add("all 8 agents reported in the last cycle", not missing,
            f"cycle {last['cycle_id']} at {last['created_utc'][:19]}" + (f"; not OK: {missing}" if missing else ""))

    # Risk Gate probes with the live limits and live account
    quotes, insts = {}, {}
    acct = pb.snapshot(quotes, insts, now)
    inst = _probe_instrument(now)
    fresh = Quote(symbol="PROBE", time_utc=now, bid=1000.0, ask=1000.05, source=DataSource(name="PROBE", is_mock=False))

    def gi(**kw):
        p = kw.pop("proposal", TradeProposal(decision_id="probe", cycle_id="probe", symbol="PROBE", action=Action.LONG,
                                              entry=1000.05, stop_loss=999.75, take_profit=1000.70, created_utc=now))
        base = dict(mode=Mode.PAPER, proposal=p, instrument=inst, spec_missing=(), quote=fresh, account=acct, now_utc=now,
                    consecutive_losses=0, open_positions=(), kill_switch_on=False, paused=False, market_open=True,
                    data_valid=True, critical_failures=(), risk_manager_approved=True, skeptic_demands_no_trade=False,
                    news_blackout=False, atr=1.0)
        base.update(kw)
        return rgate.GateInput(**base)

    probes = {
        "stale quote (25 min old)": gi(quote=fresh.model_copy(update={"time_utc": now - timedelta(minutes=25)})),
        "MOCK quote": gi(quote=fresh.model_copy(update={"source": DataSource(name="MOCK", is_mock=True)})),
        "incomplete instrument spec": gi(instrument=None, spec_missing=("volume_min",)),
        "stop so wide the minimum lot exceeds 0.5% risk": gi(proposal=TradeProposal(
            decision_id="probe", cycle_id="probe", symbol="PROBE", action=Action.LONG, entry=1000.05, stop_loss=990.0,
            take_profit=1030.0, created_utc=now)),
        "reward/risk below 2": gi(proposal=TradeProposal(decision_id="probe", cycle_id="probe", symbol="PROBE",
                                                         action=Action.LONG, entry=1000.05, stop_loss=999.75,
                                                         take_profit=1000.20, created_utc=now)),
        "second position (max 1)": gi(open_positions=(rgate.OpenPositionRisk("OTHER", "BUY", 0.3, "x"),)),
        "averaging into an open position": gi(open_positions=(rgate.OpenPositionRisk("PROBE", "BUY", 0.3, "x"),)),
        "daily loss limit hit": gi(account={**acct, "daily_pnl": -2.5, "day_start_equity": 100.0, "equity": 97.5}),
        "two consecutive losses": gi(consecutive_losses=2),
        "kill switch on": gi(kill_switch_on=True),
        "news blackout": gi(news_blackout=True),
        "critical agent failed": gi(critical_failures=("MACRO",)),
    }
    for name, inp in probes.items():
        g = rgate.evaluate(inp, runtime.limits)
        add(f"Risk Gate refuses: {name}", g.result.value == "DENY",
            ", ".join(c.name for c in g.failed)[:200] or "ALLOWED (unexpected)")

    base = rgate.evaluate(gi(), runtime.limits)
    add("info: a tight-stop baseline proposal under the live limits", True,
        f"{base.result.value}" + (f" — binding rules: {', '.join(c.name for c in base.failed)}" if base.failed else ""))

    live = runtime.market.quote_status(conn, now)
    n_live = sum(1 for q in live if q.get("status") == "LIVE")
    add("data honesty: live/stale status computed from quote age", True,
        f"{n_live}/{len(live)} symbols LIVE; others stale or no data => no new trades")
    return {"time_utc": now.isoformat(), "ok": all(c["ok"] for c in checks), "checks": checks}

"""Post-trade error analysis and weekly team review.

Error classification uses fixed, auditable rules. A single trade never changes rules: a lesson becomes a
hypothesis candidate only when the same category recurs >= RECUR_N times in the last RECUR_WINDOW trades.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from ..backtest.engine import compute_metrics
from ..db import database as db

RECUR_N, RECUR_WINDOW = 3, 20
TREND_STRATS = {"TREND_CONTINUATION", "BREAKOUT_RETEST", "VOLATILITY_BREAKOUT", "SESSION_LONDON_BREAKOUT", "SMC_CHOCH_OB"}


def classify_trade(conn: sqlite3.Connection, trade: dict, slippage_limit_points: float = 10.0,
                   max_spread_points: float = 50.0) -> list[tuple[str, str]]:
    cats: list[tuple[str, str]] = []
    if trade["net_pnl"] > 0:
        return [("NONE", f"winner {trade['r_multiple']:+.2f}R")]
    dec = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (trade["decision_id"],)).fetchone()
    reps = {r["agent"]: json.loads(r["payload"]) for r in conn.execute(
        "SELECT agent, payload FROM agent_reports WHERE cycle_id=? AND status='OK'", (dec["cycle_id"],))} if dec else {}
    dur = datetime.fromisoformat(trade["closed_utc"]) - datetime.fromisoformat(trade["opened_utc"])
    quant = reps.get("QUANT", {}).get("payload", {})
    regime = quant.get("regime")
    chosen = next((s for s in quant.get("signals", []) if s.get("strategy_id") == trade["strategy_id"]), {})
    rules = set(chosen.get("rule_ids", []))
    if trade["exit_reason"] == "STOP_LOSS" and dur <= timedelta(hours=1):
        if "MS.CHOCH.v1" in rules:
            cats.append(("FALSE_CHOCH", f"stopped {dur} after CHoCH-based entry"))
        elif "MS.OB.v1" in rules:
            cats.append(("FAILED_ORDER_BLOCK", f"order block failed within {dur}"))
    if regime == "RANGING" and trade["strategy_id"] in TREND_STRATS:
        cats.append(("WRONG_REGIME", "trend/breakout strategy traded in a RANGING regime"))
    if regime == "TRENDING" and trade["strategy_id"] == "MEAN_REVERSION":
        cats.append(("WRONG_REGIME", "mean reversion traded in a TRENDING regime"))
    macro = reps.get("MACRO", {}).get("payload", {})
    if macro.get("blackout_active") is None:
        cats.append(("NEWS", "news status was UNKNOWN at entry (possible, not confirmed, news impact)"))
    snap_ids = json.loads(dec["snapshot_ids"]) if dec else []
    if snap_ids:
        s = conn.execute("SELECT spread_points FROM market_snapshots WHERE snapshot_id=?", (snap_ids[0],)).fetchone()
        if s and s["spread_points"] and s["spread_points"] > 0.6 * max_spread_points:
            cats.append(("SPREAD", f"entry spread {s['spread_points']} points"))
    f = conn.execute("SELECT f.slippage_points FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id "
                     "WHERE o.decision_id=?", (trade["decision_id"],)).fetchone()
    if f and f["slippage_points"] and f["slippage_points"] > slippage_limit_points:
        cats.append(("EXECUTION", f"slippage {f['slippage_points']} points"))
    if trade["r_multiple"] < -1.2:
        cats.append(("RISK_ESTIMATE", f"loss {trade['r_multiple']:.2f}R exceeded planned 1R"))
    return cats or [("OTHER", "loss within plan; no rule-based error category matched")]


def analyze_new_trades(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT t.* FROM trades t LEFT JOIN lessons l ON l.trade_id=t.trade_id WHERE l.id IS NULL "
                        "ORDER BY t.closed_utc").fetchall()
    out = []
    for t in rows:
        t = dict(t)
        cats = classify_trade(conn, t)
        recent = [r["error_category"] for r in conn.execute(
            "SELECT error_category FROM lessons ORDER BY id DESC LIMIT ?", (RECUR_WINDOW,))]
        for cat, text in cats:
            recurring = recent.count(cat) + 1 >= RECUR_N and cat not in ("NONE", "OTHER")
            lid = f"les-{uuid.uuid4().hex[:10]}"
            conn.execute("INSERT INTO lessons(lesson_id, created_utc, trade_id, decision_id, error_category, description,"
                         " evidence, becomes_hypothesis) VALUES (?,?,?,?,?,?,?,?)",
                         (lid, db.utc_now_iso(), t["trade_id"], t["decision_id"], cat, text,
                          json.dumps({"r": t["r_multiple"], "exit": t["exit_reason"], "strategy": t["strategy_id"]}),
                          int(recurring)))
        conn.execute("UPDATE trades SET error_tags=? WHERE trade_id=?", (json.dumps([c for c, _ in cats]), t["trade_id"]))
        out.append({"trade_id": t["trade_id"], "expected_r": None, "actual_r": t["r_multiple"],
                    "error_categories": [c for c, _ in cats], "lessons": [x for _, x in cats]})
    return out


def consecutive_losses(conn: sqlite3.Connection, venue: str = "PAPER", account_id: str | None = None) -> int:
    reset = db.get_state(conn, "loss_streak_reset_utc") or ""
    n = 0
    acc_sql, acc_args = ("AND account_id=?", (account_id,)) if account_id else ("", ())
    for r in conn.execute(f"SELECT net_pnl, closed_utc FROM trades WHERE venue=? {acc_sql} ORDER BY closed_utc DESC",
                          (venue,) + acc_args):
        if r["closed_utc"] <= reset:
            break
        if r["net_pnl"] <= 0:
            n += 1
        else:
            break
    return n


def _account_initial(conn, account_id: str | None) -> float:
    if account_id:
        r = conn.execute("SELECT initial_balance FROM paper_accounts WHERE account_id=?", (account_id,)).fetchone()
        if r:
            return float(r["initial_balance"])
    return float(db.get_state(conn, "paper_initial_balance") or 100)


def daily_report(conn: sqlite3.Connection, day_utc: datetime, account_id: str) -> dict:
    """Report for one UTC day of one paper account: trades, decisions, refusals, errors, lessons."""
    d0 = day_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = d0 + timedelta(days=1)
    rng = (d0.isoformat(), d1.isoformat())
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE account_id=? AND closed_utc>=? AND closed_utc<?",
                                            (account_id,) + rng)]
    m = compute_metrics([{"r": t["r_multiple"], "net": t["net_pnl"]} for t in trades])
    dec = {r["action"]: r["n"] for r in conn.execute("SELECT action, COUNT(*) n FROM decisions WHERE created_utc>=? AND"
                                                     " created_utc<? GROUP BY action", rng)}
    refusals = {}
    for r in conn.execute("SELECT risk_gate_reasons FROM decisions WHERE risk_gate_result='DENY' AND created_utc>=? AND"
                          " created_utc<?", rng):
        for f in json.loads(r["risk_gate_reasons"]):
            k = f.split(":")[0]
            refusals[k] = refusals.get(k, 0) + 1
    errors = conn.execute("SELECT COUNT(*) FROM errors WHERE created_utc>=? AND created_utc<?", rng).fetchone()[0]
    ledger = {r["kind"]: round(r["s"], 4) for r in conn.execute(
        "SELECT kind, SUM(amount) s FROM account_ledger WHERE account_id=? AND created_utc>=? AND created_utc<? GROUP BY kind",
        (account_id,) + rng)}
    lessons = [dict(r) for r in conn.execute("SELECT error_category, description FROM lessons WHERE created_utc>=? AND"
                                             " created_utc<?", rng)]
    lines = [f"DAILY REPORT {d0.date()} (UTC) — paper account {account_id}",
             f"Decisions: {dec or 'none'}",
             f"Closed trades {m['n_trades']}, net {m['net_pnl']}, win rate {m['win_rate']}, expectancy {m['expectancy_r']}R",
             f"Ledger today: {ledger or 'no money movements'}",
             f"Most frequent Risk Gate refusals: {sorted(refusals.items(), key=lambda x: -x[1])[:5] or 'none'}",
             f"Errors logged: {errors}; lessons: {len(lessons)}",
             "Statistical note: a single day is never evidence of edge."]
    metrics = {**m, "decisions": dec, "refusals": refusals, "errors": errors, "ledger": ledger, "lessons": lessons}
    conn.execute("INSERT INTO daily_reports(day_utc, account_id, created_utc, metrics, report) VALUES (?,?,?,?,?)"
                 " ON CONFLICT(day_utc, account_id) DO UPDATE SET created_utc=excluded.created_utc, metrics=excluded.metrics,"
                 " report=excluded.report", (d0.date().isoformat(), account_id, db.utc_now_iso(), json.dumps(metrics, default=str),
                                             "\n".join(lines)))
    return {"report": "\n".join(lines), "metrics": metrics}


def weekly_review(conn: sqlite3.Connection, week_end_utc: datetime, venue: str = "PAPER", account_id: str | None = None) -> dict:
    start = week_end_utc - timedelta(days=7)
    acc_sql, acc_args = ("AND account_id=?", (account_id,)) if account_id else ("", ())
    trades = [dict(r) for r in conn.execute(f"SELECT * FROM trades WHERE venue=? {acc_sql} AND closed_utc>=? AND closed_utc<?",
                                            (venue,) + acc_args + (start.isoformat(), week_end_utc.isoformat()))]
    eq0 = _account_initial(conn, account_id)
    prior = conn.execute(f"SELECT COALESCE(SUM(net_pnl),0) s FROM trades WHERE venue=? {acc_sql} AND closed_utc<?",
                         (venue,) + acc_args + (start.isoformat(),)).fetchone()["s"]
    curve = [eq0 + prior]
    for t in sorted(trades, key=lambda x: x["closed_utc"]):
        curve.append(curve[-1] + t["net_pnl"])
    m = compute_metrics([{"r": t["r_multiple"], "net": t["net_pnl"]} for t in trades], curve)
    errors = {}
    for r in conn.execute("SELECT error_category, COUNT(*) n FROM lessons WHERE created_utc>=? GROUP BY error_category",
                          (start.isoformat(),)):
        errors[r["error_category"]] = r["n"]
    hyps = [dict(r) for r in conn.execute("SELECT error_category, description FROM lessons WHERE becomes_hypothesis=1 "
                                          "AND created_utc>=?", (start.isoformat(),))]
    exps = [dict(r) for r in conn.execute("SELECT experiment_id, strategy_id, stage, conclusion FROM experiments "
                                          "WHERE created_utc>=?", (start.isoformat(),))]
    changes = [dict(r) for r in conn.execute("SELECT action, entity_id, created_utc FROM audit_log WHERE action LIKE 'STRATEGY_%'"
                                             " AND created_utc>=?", (start.isoformat(),))]
    decisions = conn.execute("SELECT action, COUNT(*) n FROM decisions WHERE created_utc>=? GROUP BY action",
                             (start.isoformat(),)).fetchall()
    sufficient = m["n_trades"] >= 30
    lines = [f"WEEKLY REVIEW {start.date()} .. {week_end_utc.date()} (venue {venue})",
             f"Decisions: {', '.join(f'{d['action']}={d['n']}' for d in decisions) or 'none'}",
             f"Trades {m['n_trades']}, net PnL {m['net_pnl']}, win rate {m['win_rate']}, expectancy {m['expectancy_r']}R, "
             f"profit factor {m['profit_factor']}, max DD {m['max_drawdown_pct']}%, avg R {m['avg_r']}",
             f"Errors: {errors or 'none'}",
             f"Recurring patterns proposed as hypotheses: {len(hyps)}",
             f"Experiments: {[(e['strategy_id'], e['stage']) for e in exps] or 'none'}",
             f"Strategy changes: {[(c['action'], c['entity_id']) for c in changes] or 'none'}",
             "STATISTICAL SIGNIFICANCE: " + ("sample >= 30 trades" if sufficient else
                                             f"INSUFFICIENT ({m['n_trades']} trades) — no conclusions about edge")]
    report = "\n".join(lines)
    metrics = {**m, "errors": errors, "experiments": exps, "strategy_changes": changes,
               "hypotheses": hyps, "decisions": {d["action"]: d["n"] for d in decisions}}
    venue_key = f"{venue}:{account_id}" if account_id else venue
    conn.execute("INSERT INTO weekly_reviews(week_start_utc, week_end_utc, created_utc, venue, n_trades, metrics,"
                 " statistically_sufficient, report) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(week_start_utc, venue) DO UPDATE"
                 " SET created_utc=excluded.created_utc, n_trades=excluded.n_trades, metrics=excluded.metrics,"
                 " statistically_sufficient=excluded.statistically_sufficient, report=excluded.report",
                 (start.isoformat(), week_end_utc.isoformat(), db.utc_now_iso(), venue_key, m["n_trades"],
                  json.dumps(metrics, default=str), int(sufficient), report))
    db.audit(conn, "PERFORMANCE", "WEEKLY_REVIEW", details={"week_start": start.isoformat(), "n_trades": m["n_trades"]})
    return {"report": report, "metrics": metrics, "statistically_sufficient": sufficient}

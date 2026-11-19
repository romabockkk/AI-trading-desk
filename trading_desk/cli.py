"""Command line for the AI Trading Desk (OKX DEMO).

    python -m trading_desk cycle                  # one research cycle per instrument (no orders)
    python -m trading_desk cycle --execute        # ... and send APPROVED trades to OKX DEMO (asks first)
    python -m trading_desk run --execute --auto-approve   # loop on every M15 close, demo orders
    python -m trading_desk backtest --days 90     # historical test with the same rules
    python -m trading_desk status                 # account, open trades, protections, journal check
    python -m trading_desk kill "reason" | unkill # emergency stop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .config import Settings
from .models import Decision


def _print_cycle(res) -> None:
    print(f"\n=== {res.inst_id} — {res.decision.value} ===")
    print(res.summary)
    for r in res.reports:
        obj = f" · {len(r.objections)} objection(s)" if r.objections else ""
        print(f"  {r.agent:<20} {r.stance.value:<12}{obj}")
    if res.proposal:
        p = res.proposal
        print(f"  Proposal: {p.direction} entry {p.entry:g} SL {p.stop_loss:g} TP {p.take_profit:g} "
              f"R:R {p.rr:.2f} size {p.position_size_contracts} ct risk {p.risk_usd} USDT")
    if res.execution:
        print(f"  Execution: {json.dumps(res.execution, default=str)}")


def _ask(res) -> bool:
    p = res.proposal
    print(f"\nAPPROVAL REQUIRED (OKX DEMO): {p.direction} {p.inst_id} {p.position_size_contracts} contracts "
          f"@ {p.entry:g}, SL {p.stop_loss:g}, TP {p.take_profit:g}, risk {p.risk_usd} USDT")
    try:
        return input("Send this DEMO order? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def cmd_cycle(s: Settings, args) -> int:
    from .orchestrator import Desk
    desk = Desk(s)
    if s.has_demo_credentials:
        for e in desk.reconcile():
            print("reconcile:", json.dumps(e, default=str))
    approve = None if args.auto_approve else _ask
    for inst in s.inst_ids:
        try:
            res = desk.cycle(inst, execute=args.execute, approve=approve)
        except Exception as exc:  # noqa: BLE001 — AGENT_WORKFLOW §7: report failures clearly, never trade on them
            print(f"\n=== {inst} — CYCLE FAILED: {type(exc).__name__}: {exc} → NOT ENOUGH DATA, no orders ===")
            desk.journal.append("cycle_failed", {"inst_id": inst, "error": f"{type(exc).__name__}: {exc}"})
            continue
        _print_cycle(res)
        if res.decision == Decision.APPROVED_FOR_DEMO and res.execution and res.execution.get("status") == "submitted":
            break  # max 1 simultaneous position
    print(f"\nReports: {s.workspace_dir / 'cycles'}  ·  Journal: {s.state_dir / 'journal.jsonl'}")
    return 0


def cmd_run(s: Settings, args) -> int:
    print("Desk loop started (OKX DEMO). Ctrl+C to stop. Runs ~20s after every M15 close.")
    while True:
        now = time.time()
        nxt = (now // 900 + 1) * 900 + 20
        if not args.now:
            time.sleep(max(0, nxt - now))
        args.now = False
        print(f"\n##### cycle at {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC #####")
        cmd_cycle(s, args)
        if args.once:
            return 0


def cmd_backtest(s: Settings, args) -> int:
    from .backtest.engine import render_report, run_backtest, trades_to_dicts
    from .data.market_data import fetch_history_m15
    from .data.okx_client import OKXClient
    client = OKXClient(s.okx_base_url)
    out_dir = s.state_dir / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir = s.workspace_dir / "backtests"
    rep_dir.mkdir(parents=True, exist_ok=True)
    for inst in (args.inst or s.inst_ids):
        cache = out_dir / f"{inst}_m15_{args.days}d.csv"
        if args.cached and cache.exists():
            import pandas as pd
            m15 = pd.read_csv(cache, index_col=0, parse_dates=True)
            print(f"Using cached candles {cache}")
        else:
            print(f"Downloading {args.days} days of M15 candles for {inst} …")
            m15 = fetch_history_m15(client, inst, args.days, progress=True)
            m15.to_csv(cache)
        res = run_backtest(m15, inst, client.instrument(inst), s, start_equity=args.equity)
        summary = res.summary()
        summary["strategy_version"] = s.strategy.version
        summary["data_source"] = "OKX /api/v5/market/history-candles (15m), H1/H4 resampled"
        (out_dir / f"{inst}.json").write_text(json.dumps({**summary, "trades": trades_to_dicts(res.trades)},
                                                         indent=2, default=str))
        report = render_report(summary, res.trades, s.strategy.version)
        (rep_dir / f"{inst}.md").write_text(report)
        print(report.split("## Trades")[0])
    return 0


def cmd_status(s: Settings, args) -> int:
    from .orchestrator import Desk
    from .protections import check_protections
    desk = Desk(s)
    acct = desk.account_state()
    print(f"Mode: OKX DEMO · credentials {'configured' if s.has_demo_credentials else 'NOT configured (analysis only)'}")
    print(f"Equity: {acct.equity:,.2f} USDT ({'verified' if acct.verified else 'assumed'}) · available {acct.available:,.2f}")
    print(f"Open positions: {len(acct.open_positions)} · pending orders: {len(acct.pending_orders)}")
    prot = check_protections(desk.journal.closed_trades(), datetime.now(timezone.utc), s.risk, s.state_dir / "KILL")
    print("Protections:", "; ".join(prot.reasons) if prot.blocked else "clear")
    ok, msg = desk.journal.verify()
    closed = desk.journal.closed_trades()
    r = [t["result_r"] for t in closed if t.get("result_r") is not None]
    print(f"Journal: {msg} · closed demo trades {len(closed)}" + (f" · avg {sum(r) / len(r):+.2f}R" if r else ""))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trading_desk", description="AI Trading Desk — OKX DEMO only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("cycle", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--execute", action="store_true", help="send APPROVED trades to OKX DEMO")
        sp.add_argument("--auto-approve", action="store_true", help="skip the interactive y/N approval (demo)")
        if name == "run":
            sp.add_argument("--now", action="store_true", help="run the first cycle immediately")
            sp.add_argument("--once", action="store_true")
    bt = sub.add_parser("backtest")
    bt.add_argument("--days", type=int, default=90)
    bt.add_argument("--equity", type=float, default=10_000.0)
    bt.add_argument("--inst", nargs="*")
    bt.add_argument("--cached", action="store_true", help="reuse downloaded candles")
    sub.add_parser("status")
    k = sub.add_parser("kill")
    k.add_argument("reason", nargs="?", default="manual")
    sub.add_parser("unkill")
    sub.add_parser("verify")
    args = ap.parse_args(argv)
    s = Settings.load()
    if args.cmd == "cycle":
        return cmd_cycle(s, args)
    if args.cmd == "run":
        return cmd_run(s, args)
    if args.cmd == "backtest":
        return cmd_backtest(s, args)
    if args.cmd == "status":
        return cmd_status(s, args)
    if args.cmd in ("kill", "unkill"):
        f = s.state_dir / "KILL"
        f.parent.mkdir(parents=True, exist_ok=True)
        if args.cmd == "kill":
            f.write_text(args.reason + "\n")
            print(f"Kill switch ON ({f}). No new orders will be sent.")
        else:
            f.unlink(missing_ok=True)
            print("Kill switch OFF.")
        return 0
    if args.cmd == "verify":
        from .journal import Journal
        ok, msg = Journal(s.state_dir / "journal.jsonl").verify()
        print(msg)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""CLI: python -m tradedesk {init-db|status|verify-audit|fetch-history}"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from .config import get_settings, load_risk_limits
from .db import database as db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradedesk")
    ap.add_argument("command", choices=["init-db", "status", "verify-audit", "fetch-history", "research"])
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between requests (be polite)")
    args = ap.parse_args(argv)
    s = get_settings()
    conn = db.init_db(s.db_path())
    if args.command == "init-db":
        print(json.dumps({"db": str(s.db_path()), "migrations": db.schema_version(conn)}, indent=2))
    elif args.command == "status":
        lim = load_risk_limits(s.tradedesk_risk_limits_file)
        print(json.dumps({"settings": s.public_dict(), "risk_limits_sha256": lim.sha256,
                          "kill_switch": db.get_state(conn, "kill_switch"), "paused": db.get_state(conn, "paused")}, indent=2))
    elif args.command == "fetch-history":
        from .marketdata.dukascopy_history import DukascopyHistory, download
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        res = download(conn, args.symbol, end - timedelta(days=args.days), end, DukascopyHistory(pause_s=args.pause),
                       progress=lambda d, n: print(f"{d} {n} M1 bars", flush=True))
        print(json.dumps(res, indent=2))
    elif args.command == "research":
        print(json.dumps(research(conn, args.symbol, args.days), indent=2, default=str))
    else:
        ok, bad = db.verify_audit_chain(conn)
        print(json.dumps({"audit_chain_ok": ok, "first_bad_id": bad}))
        return 0 if ok else 1
    return 0


def research(conn, symbol: str, days: int) -> dict:
    """Walk-forward OOS evidence for every strategy on STORED REAL history, plus a $100 in-sample backtest."""
    from .backtest.engine import BacktestConfig, run_backtest
    from .data import service as dsvc
    from .learning import experiments, registry
    from .schemas.market import Timeframe
    from .strategies.library import ALL_STRATEGIES
    registry.ensure_registered(conn)
    end = datetime.now(timezone.utc)
    m5, prov = dsvc.history(conn, symbol, Timeframe.M5, end - timedelta(days=days), end)
    if not m5:
        return {"error": f"no stored real {symbol} history"}
    spec = dsvc.research_spec(conn, symbol)
    eq0 = get_settings().tradedesk_paper_initial_balance
    lim = load_risk_limits(get_settings().tradedesk_risk_limits_file).limits
    risk, lev = lim.max_risk_per_trade_pct, lim.max_effective_leverage
    out = {"symbol": symbol, "paper_equity": eq0, "risk_pct": risk, "history_provider": prov, "m5_bars": len(m5), "from": m5[0].open_time_utc,
           "to": m5[-1].open_time_utc, "spec_for_sizing": spec.source.name, "strategies": {}}

    class _Svc:
        allow_mock_research = False
    for sid in ALL_STRATEGIES:
        ev = experiments.run_evidence(conn, _Svc(), sid, "1.0.0", symbol, end, days=days, folds=3)
        bt = run_backtest(ALL_STRATEGIES[sid](), m5, spec, BacktestConfig(initial_equity=eq0, risk_pct=risk, max_leverage=lev),
                          data_is_mock=False)
        out["strategies"][sid] = {"experiment_id": ev["experiment_id"], "oos": ev["oos_metrics"], "bootstrap": ev["bootstrap"],
                                  "positive_folds": f"{ev['positive_folds']}/{ev['n_folds']}", "conclusion": ev["conclusion"],
                                  "paper_account_in_sample": {"trades": len(bt.trades), "signals_not_executable": bt.skipped_signals,
                                                       **{k: bt.metrics()[k] for k in ("net_pnl", "max_drawdown_pct")}}}
    return out


if __name__ == "__main__":
    sys.exit(main())

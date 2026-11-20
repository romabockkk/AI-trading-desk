"""Autonomous runtime: background loops that run while the server process runs.

- monitor loop (every TRADEDESK_QUOTE_POLL_SECONDS): poll quotes for all symbols, manage open paper positions
  (SL/TP, swaps, stop-out), learn from closed trades.
- analysis loop (every TRADEDESK_CYCLE_MINUTES): one agent cycle per symbol, unless paused / kill switch.
- reports: daily report after each UTC day ends; weekly review on Mondays.

Nothing here makes the system run when the server is not running: hosting is required for 24/7 operation.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .agents.llm import LLMError, make_llm
from .agents.orchestrator import Orchestrator
from .config import Settings, load_risk_limits
from .data.service import MarketService, build_providers
from .db import database as db
from .execution.paper import PaperBroker
from .learning import registry, review
from .marketdata import specs

UTC = timezone.utc


class DeskRuntime:
    def __init__(self, settings: Settings, market: MarketService | None = None, clock=None, llm=None):
        self.settings = settings
        self.limits = load_risk_limits(settings.tradedesk_risk_limits_file)
        self.market = market or MarketService(build_providers(settings), settings.symbols,
                                              self.limits.limits.max_quote_age_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.llm_error = None
        if llm is None:
            try:
                llm = make_llm(settings.tradedesk_llm_provider, settings.tradedesk_llm_model,
                               settings.llm_api_key.get_secret_value())
            except LLMError as e:
                self.llm_error = str(e)
        self.llm = llm
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.state = {"started_utc": None, "monitor_last_utc": None, "monitor_last_result": None,
                      "analysis_last_utc": None, "analysis_last_result": None, "loop_errors": 0}
        conn = self.connect()
        try:
            registry.ensure_registered(conn)
            specs.seed(conn)
            self.paper(conn)
            last = conn.execute("SELECT details FROM audit_log WHERE action='RISK_LIMITS_LOADED' ORDER BY id DESC LIMIT 1").fetchone()
            if last is None or self.limits.sha256 not in last["details"]:
                db.audit(conn, "SYSTEM", "RISK_LIMITS_LOADED", entity="risk_limits",
                         details={"sha256": self.limits.sha256, "limits": self.limits.limits.model_dump()})
        finally:
            conn.close()

    # ------------------------------------------------------------------ helpers
    def connect(self):
        return db.init_db(self.settings.db_path())

    def paper(self, conn) -> PaperBroker:
        s = self.settings
        return PaperBroker(conn, initial_balance=s.tradedesk_paper_initial_balance,
                           slippage_points=s.tradedesk_paper_slippage_points,
                           stop_out_level_pct=s.tradedesk_paper_stop_out_level_pct)

    def orchestrator(self, conn) -> Orchestrator:
        return Orchestrator(conn, self.settings, self.limits, self.market, self.llm)

    # ------------------------------------------------------------------ units of work
    def monitor_once(self) -> dict:
        with self.lock:
            conn = self.connect()
            try:
                now = self.clock()
                self.market.poll_quotes(conn, now)
                res = self.orchestrator(conn).monitor_positions(now)
                self._reports_if_due(conn, now)
                self.state["monitor_last_utc"] = now.isoformat()
                self.state["monitor_last_result"] = {"closed": len(res["closed"]), "stale": res["stale_quotes"],
                                                     "reconcile_ok": res["reconcile"]["ok"]}
                return res
            finally:
                conn.close()

    def analyze_once(self, symbols: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        with self.lock:
            conn = self.connect()
            try:
                if db.get_state(conn, "kill_switch") == "ON":
                    return [{"skipped": "kill switch ON"}]
                orch = self.orchestrator(conn)
                for s in symbols or self.settings.symbols:
                    try:
                        out.append(orch.run_cycle(s, self.clock()))
                    except Exception as e:  # noqa: BLE001 - a failing cycle must not kill the loop
                        db.log_error(conn, "cycle", "ERROR", f"{s}: {type(e).__name__}: {e}",
                                     details={"trace": traceback.format_exc()[-2000:]})
                        out.append({"symbol": s, "error": f"{type(e).__name__}: {e}"})
                self.state["analysis_last_utc"] = self.clock().isoformat()
                self.state["analysis_last_result"] = [{"symbol": r.get("symbol"), "action": r.get("action"),
                                                       "error": r.get("error")} for r in out]
            finally:
                conn.close()
        return out

    def _reports_if_due(self, conn, now: datetime) -> None:
        acc = self.paper(conn).account_id
        yday = (now - timedelta(days=1)).date().isoformat()
        if conn.execute("SELECT 1 FROM daily_reports WHERE day_utc=? AND account_id=?", (yday, acc)).fetchone() is None:
            review.daily_report(conn, now - timedelta(days=1), acc)
        if now.weekday() == 0:
            week_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = (week_end - timedelta(days=7)).isoformat()
            if conn.execute("SELECT 1 FROM weekly_reviews WHERE week_start_utc=? AND venue=?",
                            (start, f"PAPER:{acc}")).fetchone() is None:
                review.analyze_new_trades(conn)
                review.weekly_review(conn, week_end, account_id=acc)

    # ------------------------------------------------------------------ loops
    def _loop(self, period_s: float, fn, name: str) -> None:
        while not self.stop_event.wait(period_s):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.state["loop_errors"] += 1
                try:
                    conn = self.connect()
                    db.log_error(conn, f"runtime:{name}", "ERROR", f"{type(e).__name__}: {e}",
                                 details={"trace": traceback.format_exc()[-2000:]})
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _analysis_tick(self) -> None:
        conn = self.connect()
        try:
            paused = db.get_state(conn, "paused") == "true" or db.get_state(conn, "kill_switch") == "ON"
        finally:
            conn.close()
        if not paused:
            self.analyze_once()

    def start(self) -> None:
        if self.threads or not self.settings.tradedesk_autonomous:
            return
        self.state["started_utc"] = self.clock().isoformat()
        self.threads.append(threading.Thread(target=self._loop, args=(self.settings.tradedesk_quote_poll_seconds,
                                                                      self.monitor_once, "monitor"), daemon=True))
        if self.settings.tradedesk_cycle_minutes > 0:
            self.threads.append(threading.Thread(target=self._loop, args=(self.settings.tradedesk_cycle_minutes * 60,
                                                                          self._analysis_tick, "analysis"), daemon=True))
        for t in self.threads:
            t.start()

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=5)
        self.threads = []

    def status(self) -> dict:
        return {"autonomous": self.settings.tradedesk_autonomous, "running_threads": [t.is_alive() for t in self.threads],
                "quote_poll_seconds": self.settings.tradedesk_quote_poll_seconds,
                "cycle_minutes": self.settings.tradedesk_cycle_minutes, **self.state,
                "note": "runs only while this server process runs; 24/7 requires an always-on host"}

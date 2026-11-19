"""FastAPI backend + dashboard. Every response reflects stored or computed state; nothing is invented.

Owner actions (strategy activation, experiment approval, kill-switch reset, loss-streak review) require the
X-Owner-Token header when TRADEDESK_OWNER_TOKEN is set. The server binds to 127.0.0.1 by default.
"""
from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..agents.llm import LLMError, make_llm
from ..agents.orchestrator import Orchestrator
from ..backtest.engine import BacktestConfig, run_backtest
from ..broker.status import broker_demo_status
from ..config import Settings, get_settings, load_risk_limits
from ..data.mock_feed import market_open
from ..data.resample import resample
from ..data.service import MarketDataService
from ..db import database as db
from ..execution.paper import PaperBroker
from ..learning import experiments, registry, review
from ..schemas.agents import AgentName
from ..schemas.market import Timeframe
from ..strategies.library import ALL_STRATEGIES

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class ActionBody(BaseModel):
    reason: str = ""


class ActivateBody(BaseModel):
    version: str
    reason: str


class ExperimentBody(BaseModel):
    strategy_id: str
    baseline_version: str = "1.0.0"
    candidate_params: dict = Field(default_factory=dict)
    hypothesis: str
    days: int = Field(default=150, ge=30, le=300)


class EvidenceBody(BaseModel):
    strategy_id: str
    version: str = "1.0.0"
    days: int = Field(default=150, ge=30, le=300)


class ApproveBody(BaseModel):
    new_version: str


class BacktestBody(BaseModel):
    strategy_id: str
    params: dict = Field(default_factory=dict)
    days: int = Field(default=90, ge=10, le=300)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(cur) -> list[dict]:
    out = []
    for r in cur:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    d[k] = json.loads(v)
                except ValueError:
                    pass
        out.append(d)
    return out


def create_app(settings: Settings | None = None, clock=_now) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.init_db(settings.db_path())
        limits = load_risk_limits(settings.tradedesk_risk_limits_file)
        last = conn.execute("SELECT details FROM audit_log WHERE action='RISK_LIMITS_LOADED' ORDER BY id DESC LIMIT 1").fetchone()
        if last is None or limits.sha256 not in last["details"]:
            db.audit(conn, "SYSTEM", "RISK_LIMITS_LOADED", entity="risk_limits",
                     details={"sha256": limits.sha256, "limits": limits.limits.model_dump()})
        registry.ensure_registered(conn)
        PaperBroker(conn, initial_balance=settings.tradedesk_paper_initial_balance)
        db.audit(conn, "SYSTEM", "API_STARTED", details={"mode": settings.mode.value, "version": __version__})
        conn.close()
        app.state.settings, app.state.limits = settings, limits
        app.state.data = MarketDataService(settings.tradedesk_data_source)
        try:
            app.state.llm, app.state.llm_error = make_llm(settings.tradedesk_llm_provider, settings.tradedesk_llm_model,
                                                          settings.llm_api_key.get_secret_value()), None
        except LLMError as e:
            app.state.llm, app.state.llm_error = None, str(e)
        app.state.lock = threading.Lock()
        app.state.scheduler = {"enabled": settings.tradedesk_scheduler_minutes > 0, "last_run": None, "last_result": None}
        stop = threading.Event()
        if settings.tradedesk_scheduler_minutes > 0:
            t = threading.Thread(target=_scheduler_loop, args=(app, stop), daemon=True)
            t.start()
        yield
        stop.set()

    app = FastAPI(title="AI TradFi Trading Team", version=__version__, lifespan=lifespan)

    def conn_dep(request: Request):
        conn = db.connect(request.app.state.settings.db_path())   # per request: sqlite objects must not cross threads
        try:
            yield conn
        finally:
            conn.close()

    def owner(x_owner_token: str | None = Header(default=None)) -> str:
        tok = settings.tradedesk_owner_token.get_secret_value()
        if tok and not (x_owner_token and hmac.compare_digest(tok, x_owner_token)):
            raise HTTPException(403, "owner token required")
        return "OWNER"

    def run_cycle(request_app) -> dict:
        with request_app.state.lock:
            conn = db.connect(request_app.state.settings.db_path())
            try:
                orch = Orchestrator(conn, request_app.state.settings, request_app.state.limits, request_app.state.data,
                                    request_app.state.llm)
                return orch.run_cycle(request_app.state.settings.tradedesk_symbol, clock())
            finally:
                conn.close()

    app.state.run_cycle = run_cycle

    # ------------------------------------------------------------- read endpoints
    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health(conn: sqlite3.Connection = Depends(conn_dep)):
        conn.execute("SELECT 1").fetchone()
        return {"ok": True, "version": __version__}

    def _agents(conn) -> dict:
        out = {}
        for a in AgentName:
            r = conn.execute("SELECT * FROM agent_reports WHERE agent=? ORDER BY id DESC LIMIT 1", (a.value,)).fetchone()
            if r is None:
                out[a.value] = {"status": "NO_RUN_YET", "last_report": None}
                continue
            rep = json.loads(r["payload"])
            p = rep.get("payload") or {}
            out[a.value] = {"status": r["status"], "created_utc": r["created_utc"], "cycle_id": r["cycle_id"],
                            "critical": bool(r["is_critical"]), "latency_ms": r["latency_ms"], "error": r["error"],
                            "summary": p.get("summary") or p.get("hypothesis") or p.get("status") or
                                       (p.get("verdict") and f"{p['verdict']}: {'; '.join(p.get('reasons', []))}"),
                            "llm_review": rep.get("llm_review"), "payload": p}
        return out

    @app.get("/api/status")
    def status(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        s: Settings = request.app.state.settings
        chain_ok, bad = db.verify_audit_chain(conn)
        return {
            "mode": s.mode.value,
            "execution_venue": {"PAPER": "PAPER", "BROKER_DEMO": "BROKER_DEMO"}.get(s.mode.value, "NONE"),
            "live_trading": "NOT_IMPLEMENTED_AND_FORBIDDEN",
            "data_source": {"name": request.app.state.data.source, "is_mock": True,
                            "note": "synthetic data; no real market feed reachable from this environment"},
            "broker_demo": broker_demo_status(s),
            "kill_switch": db.get_state(conn, "kill_switch"),
            "paused": db.get_state(conn, "paused") == "true",
            "scheduler": request.app.state.scheduler,
            "llm": {"provider": s.tradedesk_llm_provider, "active": request.app.state.llm is not None,
                    "error": request.app.state.llm_error},
            "schema_migrations": db.schema_version(conn),
            "audit_chain_ok": chain_ok, "audit_chain_first_bad_id": bad,
            "risk_limits": {"sha256": request.app.state.limits.sha256, **request.app.state.limits.limits.model_dump()},
            "agents": _agents(conn),
            "settings": s.public_dict(),
            "server_time_utc": clock().isoformat(),
            "market_open": market_open(clock()),
        }

    @app.get("/api/dashboard")
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        st = status(request, conn)
        sym = request.app.state.settings.tradedesk_symbol
        ds = request.app.state.data
        now = clock()
        q = ds.feed(sym).quote(now)
        spec = ds.spec(sym)
        paper = PaperBroker(conn, request.app.state.settings.tradedesk_paper_initial_balance)
        acct = paper.account_info({sym: q} if q else {}, {sym: spec}, now)
        trades = _rows(conn.execute("SELECT * FROM trades ORDER BY closed_utc DESC LIMIT 50"))
        all_tr = [dict(r) for r in conn.execute("SELECT r_multiple, net_pnl, closed_utc FROM trades WHERE venue='PAPER' ORDER BY closed_utc")]
        from ..backtest.engine import compute_metrics
        init = float(db.get_state(conn, "paper_initial_balance") or 10000)
        curve = [init]
        for t in all_tr:
            curve.append(curve[-1] + t["net_pnl"])
        stats = compute_metrics([{"r": t["r_multiple"], "net": t["net_pnl"]} for t in all_tr], curve, True)
        last_dec = _rows(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 20"))
        last_cycle = last_dec[0]["cycle_id"] if last_dec else None
        return {
            **{k: st[k] for k in ("mode", "execution_venue", "live_trading", "data_source", "broker_demo", "kill_switch",
                                  "paused", "scheduler", "llm", "audit_chain_ok", "agents", "server_time_utc", "market_open",
                                  "risk_limits")},
            "symbol": sym,
            "quote": q.model_dump(mode="json") if q else None,
            "account": acct.model_dump(mode="json"),
            "unrealized_pnl": round(acct.equity - acct.balance, 2),
            "initial_balance": init,
            "stats": stats,
            "positions": _rows(conn.execute("SELECT * FROM positions WHERE status='OPEN'")),
            "trades": trades,
            "decisions": last_dec,
            "debate": _rows(conn.execute("SELECT * FROM debates WHERE cycle_id=? ORDER BY seq", (last_cycle,))) if last_cycle else [],
            "strategies": registry.list_versions(conn),
            "experiments": _rows(conn.execute("SELECT experiment_id, created_utc, strategy_id, baseline_version, candidate_version,"
                                              " stage, conclusion FROM experiments ORDER BY id DESC LIMIT 20")),
            "errors": _rows(conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT 30")),
            "risk_events": _rows(conn.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT 30")),
            "lessons": _rows(conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT 30")),
            "weekly_reviews": _rows(conn.execute("SELECT * FROM weekly_reviews ORDER BY week_start_utc DESC LIMIT 5")),
            "instruments": [{"symbol": s, "spec_source": ds.spec(s).source.name, "asset_class": ds.spec(s).asset_class}
                            for s in ds.symbols()],
        }

    @app.get("/api/candles")
    def candles(request: Request, symbol: str = "XAUUSD", tf: str = "H1", n: int = 150):
        ds = request.app.state.data
        if symbol not in ds.symbols():
            raise HTTPException(404, "unknown symbol")
        try:
            t = Timeframe(tf)
        except ValueError:
            raise HTTPException(422, "bad timeframe") from None
        n = max(10, min(n, 500))
        now = clock()
        m5 = ds.m5(symbol, now - timedelta(seconds=t.seconds * (n + 60)), now)
        bars = resample(m5, t, now)[-n:]
        return {"symbol": symbol, "timeframe": tf, "source": "MOCK", "is_mock": True,
                "candles": [{"t": b.open_time_utc.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close} for b in bars]}

    @app.get("/api/decisions")
    def decisions(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return _rows(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)))

    @app.get("/api/cycles/{cycle_id}")
    def cycle(cycle_id: str, conn: sqlite3.Connection = Depends(conn_dep)):
        reps = [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM agent_reports WHERE cycle_id=? ORDER BY id", (cycle_id,))]
        if not reps:
            raise HTTPException(404, "unknown cycle")
        return {"cycle_id": cycle_id, "reports": reps,
                "debate": _rows(conn.execute("SELECT * FROM debates WHERE cycle_id=? ORDER BY seq", (cycle_id,))),
                "decision": _rows(conn.execute("SELECT * FROM decisions WHERE cycle_id=?", (cycle_id,)))}

    @app.get("/api/strategies")
    def strategies(conn: sqlite3.Connection = Depends(conn_dep)):
        return {"versions": registry.list_versions(conn),
                "definitions": {k: v().describe() for k, v in ALL_STRATEGIES.items()}}

    @app.get("/api/experiments/{experiment_id}")
    def experiment(experiment_id: str, conn: sqlite3.Connection = Depends(conn_dep)):
        r = _rows(conn.execute("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)))
        if not r:
            raise HTTPException(404, "unknown experiment")
        return r[0]

    @app.get("/api/audit")
    def audit_tail(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]

    @app.get("/api/errors")
    def errors(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return _rows(conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)))

    # ------------------------------------------------------------- actions
    @app.post("/api/actions/start-analysis")
    def start_analysis(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        if db.get_state(conn, "kill_switch") == "ON":
            raise HTTPException(409, "kill switch is ON")
        if db.get_state(conn, "paused") == "true":
            raise HTTPException(409, "system is paused")
        return request.app.state.run_cycle(request.app)

    @app.post("/api/actions/pause")
    def pause(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        db.set_state(conn, "paused", "true", "OWNER")
        db.audit(conn, "OWNER", "PAUSE", details={"reason": body.reason})
        return {"paused": True}

    @app.post("/api/actions/resume")
    def resume(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if db.get_state(conn, "kill_switch") == "ON":
            raise HTTPException(409, "kill switch is ON; reset it explicitly first")
        db.set_state(conn, "paused", "false", "OWNER")
        db.audit(conn, "OWNER", "RESUME", details={"reason": body.reason})
        return {"paused": False}

    @app.post("/api/actions/emergency-stop")
    def emergency_stop(request: Request, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        db.set_state(conn, "kill_switch", "ON", "OWNER")
        db.set_state(conn, "paused", "true", "OWNER")
        conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, details) VALUES (?,?,?,?)",
                     (db.utc_now_iso(), "KILL_SWITCH", "CRITICAL", json.dumps({"reason": body.reason})))
        ds, now = request.app.state.data, clock()
        paper = PaperBroker(conn)
        syms = {p["symbol"] for p in paper.open_positions()}
        quotes = {s: ds.feed(s).quote(now) for s in syms}
        closed = paper.close_all({k: v for k, v in quotes.items() if v}, {s: ds.spec(s) for s in syms}, now, "EMERGENCY_STOP")
        return {"kill_switch": "ON", "paused": True, "closed_positions": closed,
                "still_open": len(paper.open_positions())}

    @app.post("/api/actions/reset-kill-switch")
    def reset_kill(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if not body.reason.strip():
            raise HTTPException(422, "a reason is required to reset the kill switch")
        db.set_state(conn, "kill_switch", "OFF", who)
        return {"kill_switch": "OFF", "paused": db.get_state(conn, "paused") == "true"}

    @app.post("/api/actions/review")
    def run_review(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        lessons = review.analyze_new_trades(conn)
        wr = review.weekly_review(conn, clock())
        return {"new_lessons": lessons, "weekly": wr, "reconcile": PaperBroker(conn).reconcile()}

    @app.post("/api/actions/acknowledge-loss-streak")
    def ack_losses(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if not body.reason.strip():
            raise HTTPException(422, "describe the review that was done")
        db.set_state(conn, "loss_streak_reset_utc", clock().isoformat(), who)
        return {"loss_streak": review.consecutive_losses(conn)}

    @app.post("/api/paper/update")
    def paper_update(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        ds, now = request.app.state.data, clock()
        paper = PaperBroker(conn)
        closed = []
        for s in {p["symbol"] for p in paper.open_positions()}:
            closed += paper.process_bars(s, ds.m5(s, now - timedelta(days=3), now), ds.spec(s))
        return {"closed": closed, "reconcile": paper.reconcile()}

    @app.post("/api/strategies/{strategy_id}/activate")
    def activate(strategy_id: str, body: ActivateBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        try:
            return registry.activate(conn, strategy_id, body.version, who, body.reason)
        except registry.RegistryError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/strategies/{strategy_id}/deactivate")
    def deactivate(strategy_id: str, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        return registry.deactivate(conn, strategy_id, who, body.reason)

    @app.post("/api/strategies/{strategy_id}/rollback")
    def rollback(strategy_id: str, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        try:
            return registry.rollback(conn, strategy_id, who, body.reason)
        except registry.RegistryError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/research/evidence")
    def evidence(request: Request, body: EvidenceBody, conn: sqlite3.Connection = Depends(conn_dep)):
        with request.app.state.lock:
            try:
                return experiments.run_evidence(conn, request.app.state.data, body.strategy_id, body.version,
                                                request.app.state.settings.tradedesk_symbol, clock(), days=body.days)
            except registry.RegistryError as e:
                raise HTTPException(422, str(e)) from None

    @app.post("/api/research/experiments")
    def propose(request: Request, body: ExperimentBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if body.strategy_id not in ALL_STRATEGIES:
            raise HTTPException(404, "unknown strategy")
        with request.app.state.lock:
            try:
                return experiments.propose(conn, request.app.state.data, body.strategy_id, body.baseline_version,
                                           body.candidate_params, body.hypothesis,
                                           request.app.state.settings.tradedesk_symbol, clock(), days=body.days)
            except (registry.RegistryError, ValueError) as e:
                raise HTTPException(422, str(e)) from None

    @app.post("/api/research/experiments/{experiment_id}/approve")
    def approve(experiment_id: str, body: ApproveBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        try:
            return experiments.approve(conn, experiment_id, body.new_version, who)
        except registry.RegistryError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/research/backtest")
    def backtest(request: Request, body: BacktestBody):
        if body.strategy_id not in ALL_STRATEGIES:
            raise HTTPException(404, "unknown strategy")
        ds, now = request.app.state.data, clock()
        sym = request.app.state.settings.tradedesk_symbol
        try:
            strat = ALL_STRATEGIES[body.strategy_id](**body.params)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        res = run_backtest(strat, ds.m5(sym, now - timedelta(days=body.days), now), ds.spec(sym), BacktestConfig())
        return {"strategy": res.strategy, "metrics": res.metrics(), "start": res.start, "end": res.end,
                "note": "IN-SAMPLE run on MOCK data; use /api/research/evidence for walk-forward OOS",
                "trades": [t.__dict__ for t in res.trades[-50:]]}

    return app


def _scheduler_loop(app: FastAPI, stop: threading.Event) -> None:
    period = app.state.settings.tradedesk_scheduler_minutes * 60
    while not stop.wait(period):
        conn = db.connect(app.state.settings.db_path())
        try:
            if db.get_state(conn, "kill_switch") == "ON" or db.get_state(conn, "paused") == "true":
                app.state.scheduler["last_result"] = "skipped: paused or kill switch"
                continue
        finally:
            conn.close()
        t0 = time.time()
        try:
            r = app.state.run_cycle(app)
            app.state.scheduler["last_result"] = {"action": r["action"], "decision_id": r["decision_id"],
                                                  "seconds": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            conn = db.connect(app.state.settings.db_path())
            db.log_error(conn, "scheduler", "ERROR", f"cycle failed: {type(e).__name__}: {e}")
            conn.close()
            app.state.scheduler["last_result"] = f"error: {e}"
        app.state.scheduler["last_run"] = datetime.now(timezone.utc).isoformat()

"""FastAPI backend. Stage 2 scope: health, honest status, safety switches, audit.

Every response reflects real state. Unimplemented components report NOT_IMPLEMENTED.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .. import __version__
from ..broker.status import broker_demo_status
from ..config import Settings, get_settings, load_risk_limits
from ..db import database as db
from ..schemas.agents import AgentName


class ActionBody(BaseModel):
    reason: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.init_db(settings.db_path())
        limits = load_risk_limits(settings.tradedesk_risk_limits_file)
        last = conn.execute(
            "SELECT details FROM audit_log WHERE action='RISK_LIMITS_LOADED' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is None or limits.sha256 not in last["details"]:
            db.audit(conn, "SYSTEM", "RISK_LIMITS_LOADED", entity="risk_limits",
                     details={"sha256": limits.sha256, "limits": limits.limits.model_dump()})
        db.audit(conn, "SYSTEM", "API_STARTED", details={"mode": settings.mode.value, "version": __version__})
        conn.close()
        app.state.settings = settings
        app.state.limits = limits
        yield

    app = FastAPI(title="AI TradFi Trading Team", version=__version__, lifespan=lifespan)

    def conn_dep(request: Request):
        # One connection per request: sqlite3 connections must not cross threads.
        conn = db.connect(request.app.state.settings.db_path())
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/health")
    def health(conn: sqlite3.Connection = Depends(conn_dep)):
        conn.execute("SELECT 1").fetchone()
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        s: Settings = request.app.state.settings
        chain_ok, bad = db.verify_audit_chain(conn)
        return {
            "mode": s.mode.value,
            "execution_venue": "PAPER" if s.mode.value == "PAPER" else
                               ("BROKER_DEMO" if s.mode.value == "BROKER_DEMO" else "NONE"),
            "live_trading": "NOT_IMPLEMENTED_AND_FORBIDDEN",
            "broker_demo": broker_demo_status(s),
            "kill_switch": db.get_state(conn, "kill_switch"),
            "paused": db.get_state(conn, "paused") == "true",
            "schema_migrations": db.schema_version(conn),
            "audit_chain_ok": chain_ok,
            "audit_chain_first_bad_id": bad,
            "risk_limits": {"sha256": request.app.state.limits.sha256,
                            **request.app.state.limits.limits.model_dump()},
            "agents": {a.value: {"status": "NOT_IMPLEMENTED", "last_report": None} for a in AgentName},
            "settings": s.public_dict(),
        }

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
    def emergency_stop(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        db.set_state(conn, "kill_switch", "ON", "OWNER")
        db.set_state(conn, "paused", "true", "OWNER")
        conn.execute(
            "INSERT INTO risk_events(created_utc, event_type, severity, details) VALUES (?,?,?,?)",
            (db.utc_now_iso(), "KILL_SWITCH", "CRITICAL", json.dumps({"reason": body.reason})),
        )
        return {"kill_switch": "ON", "paused": True,
                "note": "no open positions exist yet; position flattening arrives with paper execution (stage 7)"}

    @app.post("/api/actions/reset-kill-switch")
    def reset_kill(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if not body.reason.strip():
            raise HTTPException(422, "a reason is required to reset the kill switch")
        db.set_state(conn, "kill_switch", "OFF", "OWNER")
        return {"kill_switch": "OFF", "paused": db.get_state(conn, "paused") == "true"}

    @app.post("/api/actions/start-analysis")
    def start_analysis():
        raise HTTPException(501, "NOT_IMPLEMENTED: agent orchestrator arrives in stage 4")

    @app.get("/api/audit")
    def audit_tail(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        limit = max(1, min(limit, 500))
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/errors")
    def errors(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        limit = max(1, min(limit, 500))
        return [dict(r) for r in conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))]

    return app


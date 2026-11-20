"""FastAPI backend + dashboard for the internal paper-trading desk.

Every number comes from the database / running simulator. Owner actions need X-Owner-Token when
TRADEDESK_OWNER_TOKEN is set. When TRADEDESK_ACCESS_TOKEN is set, every request needs it (cookie after /login,
or header X-Access-Token). Binding to a non-local address without an access token is refused by Settings.
"""
from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..backtest.engine import BacktestConfig, compute_metrics, run_backtest
from ..config import Settings, get_settings
from ..data import service as dsvc
from ..data.csv_import import CsvImportError, import_mt5_csv
from ..db import database as db
from ..execution.paper import PaperError
from ..learning import experiments, registry, review
from ..marketdata import specs as specstore
from ..risk import sizing
from ..runtime import DeskRuntime
from ..schemas.agents import AgentName
from ..schemas.market import Timeframe
from ..strategies.library import ALL_STRATEGIES

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
UTC = timezone.utc


class LoginBody(BaseModel):
    token: str


class ActionBody(BaseModel):
    reason: str = ""
    confirm: str = ""


class ActivateBody(BaseModel):
    version: str
    reason: str


class SpecBody(BaseModel):
    fields: dict
    source: str


class ExperimentBody(BaseModel):
    strategy_id: str
    baseline_version: str = "1.0.0"
    candidate_params: dict = Field(default_factory=dict)
    hypothesis: str
    symbol: str = "XAUUSD"
    days: int = Field(default=150, ge=10, le=730)


class EvidenceBody(BaseModel):
    strategy_id: str
    version: str = "1.0.0"
    symbol: str = "XAUUSD"
    days: int = Field(default=150, ge=10, le=730)


class ApproveBody(BaseModel):
    new_version: str


class BacktestBody(BaseModel):
    strategy_id: str
    params: dict = Field(default_factory=dict)
    symbol: str = "XAUUSD"
    days: int = Field(default=90, ge=5, le=730)


class CsvBody(BaseModel):
    symbol: str
    timeframe: str = "M5"
    server_tz: str
    csv_text: str = Field(max_length=60_000_000)
    filename: str = "upload.csv"


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


def create_app(settings: Settings | None = None, runtime: DeskRuntime | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or DeskRuntime(settings)
        app.state.rt = rt
        conn = rt.connect()
        db.audit(conn, "SYSTEM", "API_STARTED", details={"mode": settings.mode.value, "version": __version__,
                                                          "providers": list(settings.providers)})
        conn.close()
        if start_background:
            rt.start()
        yield
        rt.stop()

    app = FastAPI(title="AI TradFi Trading Team (paper)", version=__version__, lifespan=lifespan)
    access = settings.tradedesk_access_token.get_secret_value()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if access and request.url.path not in ("/login", "/health"):
            tok = request.cookies.get("td_access") or request.headers.get("x-access-token") or ""
            if not hmac.compare_digest(tok, access):
                if request.url.path == "/":
                    return RedirectResponse("/login", status_code=303)
                return JSONResponse({"detail": "access token required"}, status_code=401)
        return await call_next(request)

    def conn_dep(request: Request):
        conn = db.connect(request.app.state.rt.settings.db_path())   # per request: sqlite objects must not cross threads
        try:
            yield conn
        finally:
            conn.close()

    def owner(x_owner_token: str | None = Header(default=None)) -> str:
        tok = settings.tradedesk_owner_token.get_secret_value()
        if tok and not (x_owner_token and hmac.compare_digest(tok, x_owner_token)):
            raise HTTPException(403, "owner token required")
        return "OWNER"

    def rt(request: Request) -> DeskRuntime:
        return request.app.state.rt

    # ------------------------------------------------------------- pages
    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Login</title>"
                "<body style='font-family:system-ui;max-width:360px;margin:15vh auto;padding:16px'>"
                "<h3>Trading Desk</h3><input id=t type=password placeholder='access token' "
                "style='width:100%;padding:10px;font-size:16px' autocomplete=current-password>"
                "<button id=b style='margin-top:10px;padding:10px 16px;font-size:16px'>Enter</button><p id=m></p>"
                "<script>b.onclick=async()=>{const r=await fetch('/login',{method:'POST',headers:{'Content-Type':"
                "'application/json'},body:JSON.stringify({token:t.value})});if(r.ok)location='/';else m.textContent="
                "'Wrong token'}</script></body>")

    @app.post("/login")
    def login(request: Request, body: LoginBody):
        if not access or not hmac.compare_digest(body.token, access):
            raise HTTPException(401, "wrong token")
        r = JSONResponse({"ok": True})
        r.set_cookie("td_access", body.token, httponly=True, samesite="strict",
                     secure=request.url.scheme == "https", max_age=30 * 86400)
        return r

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------- read
    def _agents(conn) -> dict:
        out = {}
        for a in AgentName:
            r = conn.execute("SELECT * FROM agent_reports WHERE agent=? ORDER BY id DESC LIMIT 1", (a.value,)).fetchone()
            if r is None:
                out[a.value] = {"status": "NO_RUN_YET"}
                continue
            rep = json.loads(r["payload"])
            p = rep.get("payload") or {}
            out[a.value] = {"status": r["status"], "created_utc": r["created_utc"], "cycle_id": r["cycle_id"],
                            "critical": bool(r["is_critical"]), "latency_ms": r["latency_ms"], "error": r["error"],
                            "summary": p.get("summary") or p.get("hypothesis") or p.get("status") or
                                       (p.get("verdict") and f"{p['verdict']}: {'; '.join(p.get('reasons', []))}"),
                            "final_action": p.get("final_action"), "llm_review": rep.get("llm_review")}
        return out

    def _instruments(conn, r: DeskRuntime, now):
        quotes, insts = {}, {}
        for s in r.settings.symbols:
            lt = r.market.latest(conn, s)
            if lt:
                quotes[s] = lt["quote"]
            inst, _ = r.market.instrument(conn, s, now)
            if inst:
                insts[s] = inst
        return quotes, insts

    @app.get("/api/status")
    def status(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        r = rt(request)
        now = r.clock()
        chain_ok, bad = db.verify_audit_chain(conn)
        return {"mode": r.settings.mode.value, "execution": "INTERNAL PAPER BROKER ONLY — no broker orders exist in this code",
                "live_trading": "NOT_IMPLEMENTED_AND_FORBIDDEN", "server_time_utc": now.isoformat(),
                "kill_switch": db.get_state(conn, "kill_switch"), "paused": db.get_state(conn, "paused") == "true",
                "runtime": r.status(), "providers": _rows(conn.execute("SELECT * FROM provider_status")),
                "quotes": r.market.quote_status(conn, now),
                "llm": {"provider": r.settings.tradedesk_llm_provider, "active": r.llm is not None, "error": r.llm_error},
                "audit_chain_ok": chain_ok, "audit_chain_first_bad_id": bad, "schema_migrations": db.schema_version(conn),
                "risk_limits": {"sha256": r.limits.sha256, **r.limits.limits.model_dump()},
                "agents": _agents(conn), "settings": r.settings.public_dict()}

    @app.get("/api/dashboard")
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        r = rt(request)
        now = r.clock()
        st = status(request, conn)
        pb = r.paper(conn)
        quotes, insts = _instruments(conn, r, now)
        acct = pb.snapshot(quotes, insts, now)
        acc = pb.account_id
        positions = []
        for p in pb.open_positions():
            q, inst = quotes.get(p["symbol"]), insts.get(p["symbol"])
            positions.append({**p, "unrealized_pnl": round(pb.unrealized(p, q, inst), 4) if (q and inst) else None,
                              "mark_price": (q.bid if p["side"] == "BUY" else q.ask) if q else None,
                              "quote_age_s": round((now - q.time_utc).total_seconds(), 1) if q else None})
        all_tr = [dict(x) for x in conn.execute("SELECT r_multiple, net_pnl FROM trades WHERE account_id=? ORDER BY closed_utc", (acc,))]
        curve = [acct["initial_balance"]]
        for t in all_tr:
            curve.append(curve[-1] + t["net_pnl"])
        stats = compute_metrics([{"r": t["r_multiple"], "net": t["net_pnl"]} for t in all_tr], curve)
        last_dec = _rows(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 30"))
        last_cycle = last_dec[0]["cycle_id"] if last_dec else None
        return {**{k: st[k] for k in ("mode", "execution", "live_trading", "server_time_utc", "kill_switch", "paused", "runtime",
                                      "providers", "quotes", "llm", "audit_chain_ok", "agents", "risk_limits")},
                "account": acct, "positions": positions, "stats": stats,
                "trades": _rows(conn.execute("SELECT * FROM trades WHERE account_id=? ORDER BY closed_utc DESC LIMIT 100", (acc,))),
                "decisions": last_dec,
                "debate": _rows(conn.execute("SELECT * FROM debates WHERE cycle_id=? ORDER BY seq", (last_cycle,))) if last_cycle else [],
                "strategies": registry.list_versions(conn),
                "experiments": _rows(conn.execute("SELECT experiment_id, created_utc, strategy_id, baseline_version,"
                                                  " candidate_version, stage, conclusion FROM experiments ORDER BY id DESC LIMIT 20")),
                "errors": _rows(conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT 40")),
                "risk_events": _rows(conn.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT 30")),
                "lessons": _rows(conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT 30")),
                "daily_reports": _rows(conn.execute("SELECT * FROM daily_reports WHERE account_id=? ORDER BY day_utc DESC LIMIT 7", (acc,))),
                "weekly_reviews": _rows(conn.execute("SELECT * FROM weekly_reviews ORDER BY week_start_utc DESC LIMIT 4")),
                "specs": [specstore.status(conn, s) for s in r.settings.symbols],
                "feasibility": feasibility(request, conn)}

    @app.get("/api/feasibility")
    def feasibility(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        """Can the minimum volume respect the per-trade risk budget at the current equity? Computed, not assumed."""
        r = rt(request)
        now = r.clock()
        pb = r.paper(conn)
        quotes, insts = _instruments(conn, r, now)
        eq = pb.snapshot(quotes, insts, now)["equity"]
        budget = eq * r.limits.limits.max_risk_per_trade_pct / 100
        out = []
        for s in r.settings.symbols:
            inst, q = insts.get(s), quotes.get(s)
            row = {"symbol": s, "risk_budget_usd": round(budget, 4)}
            if inst is None:
                row["status"] = "SPEC INCOMPLETE"
                row["missing"] = specstore.status(conn, s)["missing"]
            elif q is None:
                row["status"] = "NO QUOTE"
            else:
                spread = q.ask - q.bid
                vpp = sizing.money_per_price_unit(inst.spec, inst.spec.volume_min)
                max_stop = sizing.max_stop_distance_for_budget(inst.spec, budget, spread)
                lev = inst.exposure_usd(inst.spec.volume_min, q.ask) / eq if eq > 0 else None
                row.update({"min_lot": inst.spec.volume_min, "usd_per_1_price_unit_at_min_lot": round(vpp, 6),
                            "commission_at_min_lot": round(inst.commission(inst.spec.volume_min), 4),
                            "spread": round(spread, 6), "max_stop_distance_at_min_lot": round(max_stop, 6),
                            "max_stop_in_spreads": round(max_stop / spread, 2) if spread > 0 else None,
                            "margin_at_min_lot": round(inst.margin(inst.spec.volume_min, q.ask), 4),
                            "leverage_at_min_lot": round(lev, 2) if lev else None,
                            "status": "IMPOSSIBLE" if max_stop <= 0 else
                                      ("TIGHT" if spread and max_stop / spread < 1 / r.limits.limits.max_spread_to_stop_ratio
                                       else "POSSIBLE")})
            out.append(row)
        return out

    @app.get("/api/candles")
    def candles(request: Request, symbol: str = "XAUUSD", tf: str = "H1", n: int = 150,
                conn: sqlite3.Connection = Depends(conn_dep)):
        r = rt(request)
        if symbol not in r.settings.symbols:
            raise HTTPException(404, "unknown symbol")
        try:
            t = Timeframe(tf)
        except ValueError:
            raise HTTPException(422, "bad timeframe") from None
        bars, prov = r.market.bars(conn, symbol, t, r.clock(), max(10, min(n, 500)))
        bars = bars[-max(10, min(n, 500)):]
        return {"symbol": symbol, "timeframe": tf, "provider": prov, "is_mock": prov == "MOCK",
                "candles": [{"t": b.open_time_utc.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close,
                             "closed": b.is_closed} for b in bars]}

    @app.get("/api/decisions")
    def decisions(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return _rows(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)))

    @app.get("/api/cycles/{cycle_id}")
    def cycle(cycle_id: str, conn: sqlite3.Connection = Depends(conn_dep)):
        reps = [json.loads(x["payload"]) for x in conn.execute("SELECT payload FROM agent_reports WHERE cycle_id=? ORDER BY id", (cycle_id,))]
        if not reps:
            raise HTTPException(404, "unknown cycle")
        return {"cycle_id": cycle_id, "reports": reps,
                "debate": _rows(conn.execute("SELECT * FROM debates WHERE cycle_id=? ORDER BY seq", (cycle_id,))),
                "decision": _rows(conn.execute("SELECT * FROM decisions WHERE cycle_id=?", (cycle_id,)))}

    @app.get("/api/specs")
    def get_specs(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        return [specstore.status(conn, s) for s in rt(request).settings.symbols]

    @app.get("/api/ledger")
    def ledger(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        acc = rt(request).paper(conn).account_id
        return _rows(conn.execute("SELECT * FROM account_ledger WHERE account_id=? ORDER BY id DESC LIMIT 200", (acc,)))

    @app.get("/api/audit")
    def audit_tail(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return [dict(x) for x in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]

    @app.get("/api/errors")
    def errors(limit: int = 50, conn: sqlite3.Connection = Depends(conn_dep)):
        return _rows(conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)))

    @app.get("/api/strategies")
    def strategies(conn: sqlite3.Connection = Depends(conn_dep)):
        return {"versions": registry.list_versions(conn), "definitions": {k: v().describe() for k, v in ALL_STRATEGIES.items()}}

    # ------------------------------------------------------------- operator actions
    @app.post("/api/actions/start")
    def start(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if db.get_state(conn, "kill_switch") == "ON":
            raise HTTPException(409, "kill switch is ON: reset it (owner) before starting")
        db.set_state(conn, "paused", "false", "OWNER")
        db.audit(conn, "OWNER", "START", details={"reason": body.reason})
        return {"paused": False}

    @app.post("/api/actions/pause")
    def pause(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        """Stops NEW trades and analysis cycles. Open positions keep being managed (SL/TP)."""
        db.set_state(conn, "paused", "true", "OWNER")
        db.audit(conn, "OWNER", "PAUSE", details={"reason": body.reason})
        return {"paused": True, "open_positions": "still managed by SL/TP"}

    @app.post("/api/actions/emergency-stop")
    def emergency_stop(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep)):
        """Kill switch: no new trades, no analysis, until the owner resets it. Does NOT close positions
        (use close-all-positions for that); SL/TP management continues."""
        db.set_state(conn, "kill_switch", "ON", "OWNER")
        db.set_state(conn, "paused", "true", "OWNER")
        conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, details) VALUES (?,?,?,?)",
                     (db.utc_now_iso(), "KILL_SWITCH", "CRITICAL", json.dumps({"reason": body.reason})))
        return {"kill_switch": "ON", "paused": True, "open_positions": "NOT closed; managed by SL/TP"}

    @app.post("/api/actions/close-all-positions")
    def close_all(request: Request, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if body.confirm != "CLOSE ALL":
            raise HTTPException(422, 'confirmation required: confirm="CLOSE ALL"')
        r = rt(request)
        now = r.clock()
        pb = r.paper(conn)
        syms = tuple({p["symbol"] for p in pb.open_positions()})
        if syms:
            r.market.poll_quotes(conn, now, syms + ("USDJPY",))
        quotes, insts = _instruments(conn, r, now)
        stale = [s for s in syms if s in quotes and (now - quotes[s].time_utc).total_seconds() > r.limits.limits.max_quote_age_seconds]
        fresh = {s: q for s, q in quotes.items() if s not in stale}
        closed = pb.close_all(fresh, insts, now, "MANUAL")
        db.audit(conn, who, "CLOSE_ALL_POSITIONS", details={"reason": body.reason, "closed": len(closed), "stale": stale})
        return {"closed": closed, "still_open": len(pb.open_positions()),
                "not_closed_reason": "no fresh quote" if pb.open_positions() else None}

    @app.post("/api/actions/reset-account")
    def reset_account(request: Request, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if body.confirm != "RESET":
            raise HTTPException(422, 'confirmation required: confirm="RESET"')
        r = rt(request)
        pb = r.paper(conn)
        try:
            return pb.reset(who, body.reason or "reset from dashboard")
        except PaperError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/actions/reset-kill-switch")
    def reset_kill(body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if not body.reason.strip():
            raise HTTPException(422, "a reason is required")
        db.set_state(conn, "kill_switch", "OFF", who)
        return {"kill_switch": "OFF", "paused": db.get_state(conn, "paused") == "true"}

    @app.post("/api/actions/analyze-now")
    def analyze_now(request: Request, symbol: str | None = None, conn: sqlite3.Connection = Depends(conn_dep)):
        if db.get_state(conn, "kill_switch") == "ON":
            raise HTTPException(409, "kill switch is ON")
        r = rt(request)
        syms = (symbol,) if symbol else r.settings.symbols
        if any(s not in r.settings.symbols for s in syms):
            raise HTTPException(404, "unknown symbol")
        return r.analyze_once(syms)

    @app.post("/api/actions/monitor-now")
    def monitor_now(request: Request):
        res = rt(request).monitor_once()
        return {"closed": res["closed"], "stale_quotes": res["stale_quotes"], "reconcile": res["reconcile"]}

    @app.post("/api/actions/review")
    def run_review(request: Request, conn: sqlite3.Connection = Depends(conn_dep)):
        r = rt(request)
        now = r.clock()
        acc = r.paper(conn).account_id
        lessons = review.analyze_new_trades(conn)
        return {"new_lessons": lessons, "daily": review.daily_report(conn, now, acc),
                "weekly": review.weekly_review(conn, now, account_id=acc), "reconcile": r.paper(conn).reconcile()}

    @app.post("/api/actions/acknowledge-loss-streak")
    def ack_losses(request: Request, body: ActionBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        if not body.reason.strip():
            raise HTTPException(422, "describe the review that was done")
        db.set_state(conn, "loss_streak_reset_utc", rt(request).clock().isoformat(), who)
        return {"loss_streak": review.consecutive_losses(conn, account_id=rt(request).paper(conn).account_id)}

    @app.post("/api/specs/{symbol}")
    def set_spec(request: Request, symbol: str, body: SpecBody, conn: sqlite3.Connection = Depends(conn_dep),
                 who: str = Depends(owner)):
        if symbol not in rt(request).settings.symbols:
            raise HTTPException(404, "unknown symbol")
        try:
            return specstore.update(conn, symbol, body.fields, body.source, who)
        except (specstore.SpecError, ValueError) as e:
            raise HTTPException(422, str(e)) from None

    # ------------------------------------------------------------- strategies & research
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

    @app.post("/api/research/import-csv")
    def import_csv(body: CsvBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        import tempfile
        try:
            tf = Timeframe(body.timeframe)
        except ValueError:
            raise HTTPException(422, "bad timeframe") from None
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=True, encoding="utf-8") as f:
            f.write(body.csv_text)
            f.flush()
            try:
                bars, src = import_mt5_csv(f.name, body.symbol, tf, body.server_tz)
            except (CsvImportError, ValueError) as e:
                raise HTTPException(422, str(e)) from None
        from ..data.validator import validate_candles
        v = validate_candles(bars, tf, datetime.now(UTC), min_bars=1)
        n = dsvc.store_bars(conn, bars, "CSV")
        db.audit(conn, who, "HISTORY_IMPORTED", details={"symbol": body.symbol, "tf": tf.value, "bars": n,
                                                         "file": body.filename, "server_tz": body.server_tz})
        return {"stored_bars": n, "from": bars[0].open_time_utc.isoformat() if bars else None,
                "to": bars[-1].open_time_utc.isoformat() if bars else None, "validation": v.as_list()[:50]}

    @app.post("/api/research/evidence")
    def evidence(request: Request, body: EvidenceBody, conn: sqlite3.Connection = Depends(conn_dep)):
        r = rt(request)
        with r.lock:
            try:
                return experiments.run_evidence(conn, r.market, body.strategy_id, body.version, body.symbol, r.clock(),
                                                days=body.days)
            except registry.RegistryError as e:
                raise HTTPException(422, str(e)) from None

    @app.post("/api/research/experiments")
    def propose(request: Request, body: ExperimentBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if body.strategy_id not in ALL_STRATEGIES:
            raise HTTPException(404, "unknown strategy")
        r = rt(request)
        with r.lock:
            try:
                return experiments.propose(conn, r.market, body.strategy_id, body.baseline_version, body.candidate_params,
                                           body.hypothesis, body.symbol, r.clock(), days=body.days)
            except (registry.RegistryError, ValueError) as e:
                raise HTTPException(422, str(e)) from None

    @app.post("/api/research/experiments/{experiment_id}/approve")
    def approve(experiment_id: str, body: ApproveBody, conn: sqlite3.Connection = Depends(conn_dep), who: str = Depends(owner)):
        try:
            return experiments.approve(conn, experiment_id, body.new_version, who)
        except registry.RegistryError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/research/backtest")
    def backtest(request: Request, body: BacktestBody, conn: sqlite3.Connection = Depends(conn_dep)):
        if body.strategy_id not in ALL_STRATEGIES:
            raise HTTPException(404, "unknown strategy")
        r = rt(request)
        now = r.clock()
        try:
            strat = ALL_STRATEGIES[body.strategy_id](**body.params)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        m5, prov = dsvc.history(conn, body.symbol, Timeframe.M5, now - timedelta(days=body.days), now,
                                allow_mock=getattr(r.market, "allow_mock_research", False))
        if not m5:
            raise HTTPException(422, f"no stored {body.symbol} M5 history (import an MT5 CSV first)")
        spec = dsvc.research_spec(conn, body.symbol)
        res = run_backtest(strat, m5, spec, BacktestConfig(initial_equity=100.0), data_is_mock=prov == "MOCK")
        return {"strategy": res.strategy, "history_provider": prov, "spec": spec.source.name, "metrics": res.metrics(),
                "start": res.start, "end": res.end, "note": "IN-SAMPLE run; use /api/research/evidence for walk-forward OOS",
                "trades": [t.__dict__ for t in res.trades[-50:]]}

    return app

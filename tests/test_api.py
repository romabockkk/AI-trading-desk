from fastapi.testclient import TestClient

from tradedesk.api.app import create_app


def client(settings):
    return TestClient(create_app(settings))


def test_health_and_status_are_honest(settings):
    with client(settings) as c:
        assert c.get("/health").json()["ok"] is True
        s = c.get("/api/status").json()
        assert s["mode"] == "PAPER"
        assert s["live_trading"] == "NOT_IMPLEMENTED_AND_FORBIDDEN"
        assert s["broker_demo"]["status"] == "BLOCKED"
        assert s["broker_demo"]["connected"] is False
        assert s["audit_chain_ok"] is True
        assert s["risk_limits"]["max_risk_per_trade_pct"] == 0.5
        assert all(v["status"] == "NO_RUN_YET" for v in s["agents"].values())
        assert len(s["agents"]) == 8 and s["data_source"]["is_mock"] is True


def test_emergency_stop_flow(settings):
    with client(settings) as c:
        r = c.post("/api/actions/emergency-stop", json={"reason": "test"}).json()
        assert r["kill_switch"] == "ON"
        s = c.get("/api/status").json()
        assert s["kill_switch"] == "ON" and s["paused"] is True
        assert c.post("/api/actions/resume", json={}).status_code == 409
        assert c.post("/api/actions/reset-kill-switch", json={"reason": ""}).status_code == 422
        assert c.post("/api/actions/reset-kill-switch", json={"reason": "checked"}).json()["kill_switch"] == "OFF"
        assert c.post("/api/actions/resume", json={}).json()["paused"] is False
        actions = [a["action"] for a in c.get("/api/audit?limit=100").json()]
        assert "PAUSE" not in actions and "RESUME" in actions and "SET_STATE" in actions
        assert c.get("/api/status").json()["audit_chain_ok"] is True


def test_start_analysis_runs_real_cycle(settings):
    with client(settings) as c:
        r = c.post("/api/actions/start-analysis")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] in ("NO_TRADE", "LONG", "SHORT") and len(body["agent_status"]) == 8
        s = c.get("/api/status").json()
        assert all(v["status"] != "NO_RUN_YET" for v in s["agents"].values())
        d = c.get("/api/dashboard").json()
        assert d["decisions"][0]["decision_id"] == body["decision_id"] and d["account"]["trade_mode"] == "PAPER"
        assert c.get(f"/api/cycles/{body['cycle_id']}").json()["reports"]


def test_pause_blocks_analysis(settings):
    with client(settings) as c:
        c.post("/api/actions/pause", json={})
        assert c.post("/api/actions/start-analysis").status_code == 409


def test_owner_token_enforced(settings):
    s = settings.model_copy(update={"tradedesk_owner_token": __import__("pydantic").SecretStr("t0k")})
    with client(s) as c:
        body = {"version": "1.0.0", "reason": "x"}
        assert c.post("/api/strategies/MEAN_REVERSION/activate", json=body).status_code == 403
        assert c.post("/api/strategies/MEAN_REVERSION/activate", json=body, headers={"X-Owner-Token": "bad"}).status_code == 403
        assert c.post("/api/strategies/MEAN_REVERSION/activate", json=body, headers={"X-Owner-Token": "t0k"}).status_code == 200
        assert c.post("/api/actions/reset-kill-switch", json={"reason": "x"}).status_code == 403


def test_dashboard_page_and_candles(settings):
    with client(settings) as c:
        assert "Start Analysis" in c.get("/").text
        k = c.get("/api/candles?tf=H1&n=50").json()
        assert k["is_mock"] is True and 0 < len(k["candles"]) <= 50
        assert c.get("/api/candles?tf=X").status_code == 422


def test_backtest_and_review_endpoints(settings):
    with client(settings) as c:
        r = c.post("/api/research/backtest", json={"strategy_id": "TREND_CONTINUATION", "days": 20}).json()
        assert "IN-SAMPLE" in r["note"] and r["metrics"]["data_is_mock"] is True
        w = c.post("/api/actions/review").json()
        assert w["weekly"]["statistically_sufficient"] is False and w["reconcile"]["ok"]


def test_risk_limits_load_audited_once(settings):
    with client(settings) as c:
        pass
    with client(settings) as c:
        rows = [a for a in c.get("/api/audit?limit=100").json() if a["action"] == "RISK_LIMITS_LOADED"]
        assert len(rows) == 1

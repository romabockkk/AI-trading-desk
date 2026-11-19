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
        assert all(v["status"] == "NOT_IMPLEMENTED" for v in s["agents"].values())
        assert len(s["agents"]) == 8


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


def test_start_analysis_not_faked(settings):
    with client(settings) as c:
        assert c.post("/api/actions/start-analysis").status_code == 501


def test_risk_limits_load_audited_once(settings):
    with client(settings) as c:
        pass
    with client(settings) as c:
        rows = [a for a in c.get("/api/audit?limit=100").json() if a["action"] == "RISK_LIMITS_LOADED"]
        assert len(rows) == 1

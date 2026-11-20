from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from helpers import GOLD_TEST_SPEC, T0, market
from tradedesk.api.app import create_app
from tradedesk.runtime import DeskRuntime

NOW = T0 + timedelta(minutes=2)


def client(settings, **upd):
    s = settings.model_copy(update=upd)
    rt = DeskRuntime(s, market=market(symbols=("XAUUSD", "USDJPY")), clock=lambda: NOW)
    rt.settings = s.model_copy(update={"tradedesk_symbols": "XAUUSD,USDJPY"})
    return TestClient(create_app(s, runtime=rt, start_background=False))


def test_status_dashboard_and_1000_usd_account(settings):
    with client(settings) as c:
        assert c.get("/health").json()["ok"] is True
        s = c.get("/api/status").json()
        assert s["mode"] == "PAPER" and "no broker orders" in s["execution"]
        assert len(s["agents"]) == 8 and all(v["status"] == "NO_RUN_YET" for v in s["agents"].values())
        d = c.get("/api/dashboard").json()
        a = d["account"]
        assert a["balance"] == 1000.0 and a["equity"] == 1000.0 and a["free_margin"] == 1000.0 and a["total_pnl"] == 0
        assert d["stats"]["n_trades"] == 0
        feas = {f["symbol"]: f for f in d["feasibility"]}
        assert feas["XAUUSD"]["status"] == "SPEC INCOMPLETE"


def test_quotes_status_shows_source_and_latency(settings):
    with client(settings) as c:
        c.post("/api/actions/monitor-now")
        q = {x["symbol"]: x for x in c.get("/api/status").json()["quotes"]}
        assert q["XAUUSD"]["provider"] == "FIXTURE" and q["XAUUSD"]["status"] == "LIVE"
        assert "age_s" in q["XAUUSD"] and "delivery_delay_s" in q["XAUUSD"]


def test_analyze_now_runs_real_cycle_and_records(settings):
    with client(settings) as c:
        r = c.post("/api/actions/analyze-now?symbol=XAUUSD")
        assert r.status_code == 200, r.text
        body = r.json()[0]
        assert body["action"] == "NO_TRADE" and len(body["agent_status"]) == 8
        d = c.get("/api/dashboard").json()
        assert d["decisions"][0]["decision_id"] == body["decision_id"] and d["debate"]
        assert all(v["status"] != "NO_RUN_YET" for v in d["agents"].values())


def test_pause_start_emergency_are_distinct(settings):
    with client(settings) as c:
        assert c.post("/api/actions/pause", json={}).json()["paused"] is True
        assert c.post("/api/actions/start", json={}).json()["paused"] is False
        e = c.post("/api/actions/emergency-stop", json={"reason": "t"}).json()
        assert e["kill_switch"] == "ON" and "NOT closed" in e["open_positions"]
        assert c.post("/api/actions/start", json={}).status_code == 409
        assert c.post("/api/actions/analyze-now").status_code == 409
        assert c.post("/api/actions/reset-kill-switch", json={"reason": "checked"}).json()["kill_switch"] == "OFF"


def test_close_all_and_reset_require_confirmation(settings):
    with client(settings) as c:
        assert c.post("/api/actions/close-all-positions", json={}).status_code == 422
        assert c.post("/api/actions/close-all-positions", json={"confirm": "CLOSE ALL"}).json()["still_open"] == 0
        assert c.post("/api/actions/reset-account", json={"confirm": "yes"}).status_code == 422
        old = c.get("/api/dashboard").json()["account"]["account_id"]
        r = c.post("/api/actions/reset-account", json={"confirm": "RESET", "reason": "test"}).json()
        assert r["archived"] == old and r["balance"] == 1000.0


def test_owner_token_enforced(settings):
    with client(settings, tradedesk_owner_token=SecretStr("t0k")) as c:
        body = {"fields": {"volume_min": 0.01}, "source": "app"}
        assert c.post("/api/specs/XAUUSD", json=body).status_code == 403
        assert c.post("/api/specs/XAUUSD", json=body, headers={"X-Owner-Token": "bad"}).status_code == 403
        assert c.post("/api/specs/XAUUSD", json=body, headers={"X-Owner-Token": "t0k"}).status_code == 200
        assert c.post("/api/actions/reset-account", json={"confirm": "RESET"}).status_code == 403
        assert c.post("/api/actions/close-all-positions", json={"confirm": "CLOSE ALL"}).status_code == 403


def test_spec_entry_makes_feasibility_computable(settings):
    with client(settings) as c:
        c.post("/api/actions/monitor-now")
        r = c.post("/api/specs/XAUUSD", json={"fields": GOLD_TEST_SPEC, "source": "TEST FIXTURE"})
        assert r.json()["complete"] is True
        f = {x["symbol"]: x for x in c.get("/api/feasibility").json()}["XAUUSD"]
        assert f["min_lot"] == 0.01 and f["usd_per_1_price_unit_at_min_lot"] == pytest.approx(1.0)
        assert f["commission_at_min_lot"] == pytest.approx(0.06) and f["risk_budget_usd"] == pytest.approx(10.0)


def test_access_token_protects_everything(settings):
    with client(settings, tradedesk_access_token=SecretStr("secret-123")) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/status").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303
        assert c.post("/login", json={"token": "nope"}).status_code == 401
        assert c.post("/login", json={"token": "secret-123"}).status_code == 200
        assert c.get("/api/status").status_code == 200           # cookie set
    with client(settings, tradedesk_access_token=SecretStr("secret-123")) as c2:
        assert c2.get("/api/status", headers={"X-Access-Token": "secret-123"}).status_code == 200


def test_dashboard_page_and_candles(settings):
    with client(settings) as c:
        html = c.get("/").text
        for label in ("Start", "Pause new trades", "Emergency Stop", "Close all positions", "Reset paper account"):
            assert label in html
        k = c.get("/api/candles?symbol=XAUUSD&tf=H1&n=50").json()
        assert k["provider"] == "FIXTURE" and 0 < len(k["candles"]) <= 50
        assert c.get("/api/candles?tf=X").status_code == 422
        assert c.get("/api/candles?symbol=NOPE").status_code == 404


def test_csv_import_and_review(settings):
    with client(settings) as c:
        csv = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n" + "".join(
            f"2026.06.10\t{h:02d}:{m:02d}:00\t4000.0\t4001.0\t3999.0\t4000.5\t10\t0\t20\n" for h in range(10, 12) for m in range(0, 60, 5))
        r = c.post("/api/research/import-csv", json={"symbol": "XAUUSD", "timeframe": "M5", "server_tz": "UTC+3",
                                                     "csv_text": csv})
        assert r.status_code == 200, r.text
        assert r.json()["stored_bars"] == 24 and r.json()["from"].startswith("2026-06-10T07:00")
        w = c.post("/api/actions/review").json()
        assert w["weekly"]["statistically_sufficient"] is False and w["reconcile"]["ok"] and "DAILY REPORT" in w["daily"]["report"]


def test_self_check_on_running_system(settings):
    with client(settings) as c:
        c.post("/api/actions/analyze-now?symbol=XAUUSD")
        sc = c.get("/api/self-check").json()
        by = {x["check"]: x for x in sc["checks"]}
        assert by["initial balance is $1,000 (configured)"]["ok"] and by["balance equals ledger sum"]["ok"]
        assert by["all 8 agents reported in the last cycle"]["ok"]
        refusals = [x for x in sc["checks"] if x["check"].startswith("Risk Gate refuses")]
        assert len(refusals) == 16 and all(x["ok"] for x in refusals)
        assert sc["ok"] is True

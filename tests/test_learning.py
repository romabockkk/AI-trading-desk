import json
from datetime import timedelta

import pytest

from helpers import T0
from tradedesk.data.service import MarketDataService
from tradedesk.learning import experiments, registry, review

DS = MarketDataService(history_days=20)


def test_registry_registers_six_research_versions(conn):
    registry.ensure_registered(conn)
    v = registry.list_versions(conn)
    assert len(v) == 6 and all(x["status"] == "RESEARCH" for x in v)
    assert registry.ensure_registered(conn) == []


def test_only_owner_can_activate_and_rollback(conn):
    registry.ensure_registered(conn)
    with pytest.raises(registry.RegistryError):
        registry.activate(conn, "MEAN_REVERSION", "1.0.0", "CHIEF", "agent wants it")
    with pytest.raises(registry.RegistryError):
        registry.activate(conn, "MEAN_REVERSION", "1.0.0", "OWNER", " ")
    registry.activate(conn, "MEAN_REVERSION", "1.0.0", "OWNER", "paper test")
    assert [x["strategy_id"] for x in registry.active_versions(conn)] == ["MEAN_REVERSION"]
    registry.add_version(conn, "MEAN_REVERSION", "1.1.0", {"z": 2.5}, "1.0.0", "test", "OWNER")
    registry.activate(conn, "MEAN_REVERSION", "1.1.0", "OWNER", "rollout")
    res = registry.rollback(conn, "MEAN_REVERSION", "OWNER", "worse in paper")
    assert res["active_version"] == "1.0.0"
    st = {x["version"]: x["status"] for x in registry.list_versions(conn) if x["strategy_id"] == "MEAN_REVERSION"}
    assert st == {"1.0.0": "ACTIVE_PAPER", "1.1.0": "ROLLED_BACK"}
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='STRATEGY_ACTIVATED'").fetchone()[0] == 3


def test_evidence_and_experiment_pipeline(conn):
    registry.ensure_registered(conn)
    ev = experiments.run_evidence(conn, DS, "TREND_CONTINUATION", "1.0.0", "XAUUSD", T0, days=120, folds=3)
    assert ev["n_folds"] == 3 and "MOCK" in ev["conclusion"]
    eid, got = experiments.latest_evidence(conn, "TREND_CONTINUATION", "1.0.0")
    assert eid == ev["experiment_id"] and got["oos_metrics"]["data_is_mock"] is True
    prop = experiments.propose(conn, DS, "TREND_CONTINUATION", "1.0.0", {"min_er": 0.35, "rr": 2.5}, "stricter ER",
                               "XAUUSD", T0, days=120, folds=3)
    assert prop["stage"] in ("AWAITING_APPROVAL", "REJECTED")
    row = conn.execute("SELECT * FROM experiments WHERE experiment_id=?", (prop["experiment_id"],)).fetchone()
    assert json.loads(row["baseline_metrics"])["oos_metrics"] is not None
    if prop["stage"] == "REJECTED":
        with pytest.raises(registry.RegistryError):
            experiments.approve(conn, prop["experiment_id"], "1.1.0", "OWNER")
    else:
        with pytest.raises(registry.RegistryError):
            experiments.approve(conn, prop["experiment_id"], "1.1.0", "QUANT")
        experiments.approve(conn, prop["experiment_id"], "1.1.0", "OWNER")
        st = conn.execute("SELECT status FROM strategy_versions WHERE version='1.1.0'").fetchone()[0]
        assert st == "APPROVED_PAPER"            # approved but NOT active until the owner activates it


def _trade(conn, tid, net, r, exit_reason="STOP_LOSS", minutes=30, strat="SMC_CHOCH_OB", t=T0):
    did = f"d-{tid}"
    conn.execute("INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
                 " risk_gate_result) VALUES (?,?,?,?,?,?,?,?,?)", (did, f"c-{tid}", t.isoformat(), "PAPER", "XAUUSD",
                                                                     "LONG", 1, 0.5, "ALLOW"))
    conn.execute("INSERT INTO positions(position_id, venue, symbol, side, volume_lots, open_price, stop_loss, opened_utc,"
                 " updated_utc, status) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"p-{tid}", "PAPER", "XAUUSD", "BUY", 0.1, 1, 0.5,
                                                                         t.isoformat(), t.isoformat(), "CLOSED"))
    conn.execute("INSERT INTO trades(trade_id, position_id, decision_id, venue, symbol, side, strategy_id, opened_utc,"
                 " closed_utc, entry_price, exit_price, volume_lots, gross_pnl, net_pnl, planned_risk_money, r_multiple,"
                 " exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (tid, f"p-{tid}", did, "PAPER", "XAUUSD", "BUY", strat, t.isoformat(),
                  (t + timedelta(minutes=minutes)).isoformat(), 1, 1, 0.1, net, net, 50, r, exit_reason))


def test_error_classification_and_recurrence_threshold(conn):
    for k in range(3):
        _trade(conn, f"t{k}", -50, -1.3, t=T0 + timedelta(hours=k))
    out = review.analyze_new_trades(conn)
    assert len(out) == 3 and all("RISK_ESTIMATE" in o["error_categories"] for o in out)
    flags = [r["becomes_hypothesis"] for r in conn.execute("SELECT becomes_hypothesis FROM lessons "
                                                           "WHERE error_category='RISK_ESTIMATE' ORDER BY id")]
    assert flags == [0, 0, 1]          # a single trade never becomes a hypothesis
    assert review.analyze_new_trades(conn) == []


def test_consecutive_losses_and_owner_reset(conn):
    _trade(conn, "a", 80, 1.6, "TAKE_PROFIT", t=T0)
    _trade(conn, "b", -50, -1, t=T0 + timedelta(hours=1))
    _trade(conn, "c", -50, -1, t=T0 + timedelta(hours=2))
    assert review.consecutive_losses(conn) == 2
    from tradedesk.db import database as db
    db.set_state(conn, "loss_streak_reset_utc", (T0 + timedelta(hours=3)).isoformat(), "OWNER")
    assert review.consecutive_losses(conn) == 0


def test_weekly_review_flags_small_sample(conn):
    _trade(conn, "a", 80, 1.6, "TAKE_PROFIT")
    _trade(conn, "b", -50, -1, t=T0 + timedelta(hours=1))
    w = review.weekly_review(conn, T0 + timedelta(days=2))
    assert w["metrics"]["n_trades"] == 2 and w["statistically_sufficient"] is False
    assert "INSUFFICIENT" in w["report"] and w["metrics"]["net_pnl"] == 30
    review.weekly_review(conn, T0 + timedelta(days=2))     # upsert, no duplicate row
    assert conn.execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0] == 1

import json
import sqlite3

import pytest

from tradedesk.db import database as db


def test_all_required_tables_exist(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = set(db.EXPECTED_TABLES) - names
    assert not missing


def test_migrations_idempotent(tmp_path):
    p = tmp_path / "m.db"
    c1 = db.init_db(p)
    v1 = db.schema_version(c1)
    c1.close()
    c2 = db.init_db(p)
    assert db.schema_version(c2) == v1 == ["0001_init.sql", "0002_paper_accounts.sql"]
    assert db.migrate(c2) == []


def test_modified_migration_detected(conn):
    conn.execute("UPDATE schema_migrations SET sha256='x' WHERE name='0001_init.sql'")
    with pytest.raises(RuntimeError, match="modified"):
        db.migrate(conn)


def test_audit_log_append_only(conn):
    db.audit(conn, "SYSTEM", "TEST")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET action='X'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_audit_chain_verifies_and_detects_tamper(conn):
    for i in range(5):
        db.audit(conn, "SYSTEM", "EVT", details={"i": i})
    assert db.verify_audit_chain(conn) == (True, None)
    # Tamper by bypassing triggers
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET details='{\"i\":99}' WHERE id=3")
    ok, bad = db.verify_audit_chain(conn)
    assert not ok and bad == 3


def test_decision_with_trade_requires_stop_loss(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry,"
            " risk_gate_result) VALUES ('d1','c1','2026-01-01T00:00:00Z','PAPER','XAUUSD','LONG',2000,'ALLOW')")


def test_trade_decision_denied_by_gate_needs_reason(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
            " risk_gate_result) VALUES ('d1','c1','2026-01-01T00:00:00Z','PAPER','XAUUSD','LONG',2000,1990,'DENY')")


def test_live_mode_cannot_be_stored(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, risk_gate_result)"
            " VALUES ('d1','c1','2026-01-01T00:00:00Z','LIVE','XAUUSD','NO_TRADE','NOT_EVALUATED')")


def test_order_idempotency_key_unique(conn):
    conn.execute(
        "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
        " risk_gate_result) VALUES ('d1','c1','2026-01-01T00:00:00Z','PAPER','XAUUSD','LONG',2000,1990,'ALLOW')")
    sql = ("INSERT INTO orders(client_order_id, decision_id, venue, created_utc, updated_utc, symbol, side,"
           " order_type, volume_lots, stop_loss, status) VALUES ('o1','d1','PAPER','t','t','XAUUSD','BUY',"
           "'MARKET',0.01,1990,'PENDING_SUBMIT')")
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_order_requires_existing_decision(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders(client_order_id, decision_id, venue, created_utc, updated_utc, symbol, side,"
            " order_type, volume_lots, stop_loss, status) VALUES ('o1','nope','PAPER','t','t','XAUUSD','BUY',"
            "'MARKET',0.01,1990,'PENDING_SUBMIT')")


def test_order_cannot_be_marked_real_account(conn):
    conn.execute(
        "INSERT INTO decisions(decision_id, cycle_id, created_utc, mode, symbol, action, entry, stop_loss,"
        " risk_gate_result) VALUES ('d1','c1','2026-01-01T00:00:00Z','PAPER','XAUUSD','LONG',2000,1990,'ALLOW')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders(client_order_id, decision_id, venue, created_utc, updated_utc, symbol, side,"
            " order_type, volume_lots, stop_loss, status, broker_account_is_demo) VALUES ('o1','d1','BROKER_DEMO',"
            "'t','t','XAUUSD','BUY','MARKET',0.01,1990,'PENDING_SUBMIT',0)")


def test_invalid_json_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO errors(created_utc, component, severity, message, details)"
                     " VALUES ('t','x','ERROR','m','not json')")


def test_state_defaults_and_set_state_audited(conn):
    assert db.get_state(conn, "kill_switch") == "OFF"
    assert db.get_state(conn, "paused") == "false"
    db.set_state(conn, "kill_switch", "ON", "OWNER")
    assert db.get_state(conn, "kill_switch") == "ON"
    row = conn.execute("SELECT * FROM audit_log WHERE action='SET_STATE' ORDER BY id DESC").fetchone()
    assert json.loads(row["details"]) == {"old": "OFF", "new": "ON"}


def test_log_error(conn):
    eid = db.log_error(conn, "data", "ERROR", "feed down", details={"src": "MOCK"})
    assert conn.execute("SELECT message FROM errors WHERE id=?", (eid,)).fetchone()[0] == "feed down"

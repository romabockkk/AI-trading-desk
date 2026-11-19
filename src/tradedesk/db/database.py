"""SQLite access, forward-only migrations and the hash-chained audit log."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_PACKAGE = "tradedesk.db.migrations"
GENESIS_HASH = "0" * 64

EXPECTED_TABLES = (
    "market_snapshots", "agent_reports", "debates", "decisions", "orders", "fills",
    "positions", "trades", "risk_events", "errors", "lessons", "strategy_versions",
    "experiments", "weekly_reviews", "audit_log",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=10)  # autocommit; explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(p) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files() -> list[tuple[str, str]]:
    files = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        if entry.name.endswith(".sql"):
            files.append((entry.name, entry.read_text(encoding="utf-8")))
    return sorted(files)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Returns names applied in this call."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_utc TEXT NOT NULL)"
    )
    applied = {r["name"]: r["sha256"] for r in conn.execute("SELECT name, sha256 FROM schema_migrations")}
    newly: list[str] = []
    for name, sql in _migration_files():
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if name in applied:
            if applied[name] != digest:
                raise RuntimeError(f"migration {name} was modified after being applied; add a new migration instead")
            continue
        # executescript cannot take parameters; name/digest come from package files (escaped anyway).
        q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
        record = (f"INSERT INTO schema_migrations(name, sha256, applied_utc) "
                  f"VALUES ({q(name)}, {q(digest)}, {q(utc_now_iso())});")
        try:
            conn.executescript("BEGIN;\n" + sql + "\n" + record + "\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        newly.append(name)
    return newly


def schema_version(conn: sqlite3.Connection) -> list[str]:
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    except sqlite3.OperationalError:
        return []


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _audit_hash(prev_hash: str, created_utc: str, actor: str, action: str,
                entity: str | None, entity_id: str | None, details_json: str) -> str:
    material = "|".join([prev_hash, created_utc, actor, action, entity or "", entity_id or "", details_json])
    return hashlib.sha256(material.encode()).hexdigest()


def audit(conn: sqlite3.Connection, actor: str, action: str, *, entity: str | None = None,
          entity_id: str | None = None, details: dict | None = None) -> str:
    """Append an audit record. Returns its hash. Never pass secrets in details."""
    details_json = _canonical(details or {})
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["row_hash"] if row else GENESIS_HASH
        ts = utc_now_iso()
        h = _audit_hash(prev, ts, actor, action, entity, entity_id, details_json)
        conn.execute(
            "INSERT INTO audit_log(created_utc, actor, action, entity, entity_id, details, prev_hash, row_hash)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts, actor, action, entity, entity_id, details_json, prev, h),
        )
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    if own_tx:
        conn.execute("COMMIT")
    return h


def verify_audit_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (ok, first_bad_id)."""
    prev = GENESIS_HASH
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        if r["prev_hash"] != prev:
            return False, r["id"]
        h = _audit_hash(prev, r["created_utc"], r["actor"], r["action"], r["entity"], r["entity_id"], r["details"])
        if h != r["row_hash"]:
            return False, r["id"]
        prev = h
    return True, None


def log_error(conn: sqlite3.Connection, component: str, severity: str, message: str, *,
              cycle_id: str | None = None, details: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO errors(created_utc, component, severity, message, cycle_id, details) VALUES (?,?,?,?,?,?)",
        (utc_now_iso(), component, severity, message, cycle_id, _canonical(details or {})),
    )
    return int(cur.lastrowid)


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_state(conn: sqlite3.Connection, key: str, value: str, actor: str) -> None:
    with transaction(conn):
        old = get_state(conn, key)
        conn.execute(
            "INSERT INTO system_state(key, value, updated_utc, updated_by) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_utc=excluded.updated_utc,"
            " updated_by=excluded.updated_by",
            (key, value, utc_now_iso(), actor),
        )
        audit(conn, actor, "SET_STATE", entity="system_state", entity_id=key, details={"old": old, "new": value})


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    applied = migrate(conn)
    if applied:
        audit(conn, "SYSTEM", "MIGRATIONS_APPLIED", details={"migrations": applied})
    return conn

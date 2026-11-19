"""Strategy version registry. Only the OWNER can activate, retire or roll back versions."""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3

from ..db import database as db
from ..strategies.library import ALL_STRATEGIES

OWNER = "OWNER"


class RegistryError(ValueError):
    pass


def _code_hash(cls) -> str:
    return hashlib.sha256(inspect.getsource(cls).encode()).hexdigest()


def ensure_registered(conn: sqlite3.Connection) -> list[str]:
    added = []
    for sid, cls in ALL_STRATEGIES.items():
        row = conn.execute("SELECT 1 FROM strategy_versions WHERE strategy_id=? AND version=?", (sid, cls.version)).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO strategy_versions(strategy_id, version, created_utc, status, params, rules_text, code_sha256, notes)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, cls.version, db.utc_now_iso(), "RESEARCH", json.dumps(cls.default_params), inspect.cleandoc(cls.rules),
             _code_hash(cls), "initial research version; not proven profitable"))
        added.append(f"{sid}@{cls.version}")
    if added:
        db.audit(conn, "SYSTEM", "STRATEGIES_REGISTERED", details={"versions": added})
    return added


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM strategy_versions ORDER BY strategy_id, id")]


def active_versions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM strategy_versions WHERE status='ACTIVE_PAPER'")]


def activate(conn: sqlite3.Connection, strategy_id: str, version: str, actor: str, reason: str) -> dict:
    if actor != OWNER:
        raise RegistryError("only the OWNER may activate a strategy version")
    if not reason.strip():
        raise RegistryError("a reason is required")
    row = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?", (strategy_id, version)).fetchone()
    if row is None:
        raise RegistryError(f"unknown version {strategy_id}@{version}")
    if row["status"] in ("RETIRED", "ROLLED_BACK") and "rollback" not in reason.lower():
        raise RegistryError("re-activating a retired version requires a reason mentioning 'rollback'")
    with db.transaction(conn):
        prev = conn.execute("SELECT version FROM strategy_versions WHERE strategy_id=? AND status='ACTIVE_PAPER'",
                            (strategy_id,)).fetchall()
        conn.execute("UPDATE strategy_versions SET status='RETIRED' WHERE strategy_id=? AND status='ACTIVE_PAPER'", (strategy_id,))
        conn.execute("UPDATE strategy_versions SET status='ACTIVE_PAPER', approved_by=?, approved_utc=? "
                     "WHERE strategy_id=? AND version=?", (actor, db.utc_now_iso(), strategy_id, version))
        db.audit(conn, actor, "STRATEGY_ACTIVATED", entity="strategy_versions", entity_id=f"{strategy_id}@{version}",
                 details={"reason": reason, "previous_active": [r["version"] for r in prev]})
    return {"strategy_id": strategy_id, "active_version": version, "previous": [r["version"] for r in prev]}


def deactivate(conn: sqlite3.Connection, strategy_id: str, actor: str, reason: str) -> dict:
    if actor != OWNER:
        raise RegistryError("only the OWNER may deactivate")
    with db.transaction(conn):
        n = conn.execute("UPDATE strategy_versions SET status='RETIRED' WHERE strategy_id=? AND status='ACTIVE_PAPER'",
                         (strategy_id,)).rowcount
        db.audit(conn, actor, "STRATEGY_DEACTIVATED", entity="strategy_versions", entity_id=strategy_id,
                 details={"reason": reason, "rows": n})
    return {"strategy_id": strategy_id, "deactivated": n}


def rollback(conn: sqlite3.Connection, strategy_id: str, actor: str, reason: str) -> dict:
    """Re-activate the most recent version that was active before the current one."""
    if actor != OWNER:
        raise RegistryError("only the OWNER may roll back")
    cur = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND status='ACTIVE_PAPER'", (strategy_id,)).fetchone()
    if cur is None:
        raise RegistryError("no active version to roll back from")
    prev = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND id<? AND approved_by IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (strategy_id, cur["id"])).fetchone()
    if prev is None:
        raise RegistryError("no earlier approved version exists")
    res = activate(conn, strategy_id, prev["version"], actor, f"rollback: {reason}")
    conn.execute("UPDATE strategy_versions SET status='ROLLED_BACK' WHERE id=?", (cur["id"],))
    return res


def add_version(conn: sqlite3.Connection, strategy_id: str, version: str, params: dict, parent: str, notes: str,
                actor: str) -> None:
    if actor != OWNER:
        raise RegistryError("only the OWNER may approve a new version")
    cls = ALL_STRATEGIES[strategy_id]
    cls(**params)  # validates param names
    conn.execute("INSERT INTO strategy_versions(strategy_id, version, created_utc, status, parent_version, params, rules_text,"
                 " code_sha256, approved_by, approved_utc, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (strategy_id, version, db.utc_now_iso(), "APPROVED_PAPER", parent, json.dumps(params),
                  inspect.cleandoc(cls.rules), _code_hash(cls), actor, db.utc_now_iso(), notes))
    db.audit(conn, actor, "STRATEGY_VERSION_APPROVED", entity="strategy_versions", entity_id=f"{strategy_id}@{version}",
             details={"params": params, "parent": parent})

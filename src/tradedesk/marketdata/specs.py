"""Instrument specifications with per-field provenance. No invented values.

Seeded from config/instruments.toml; the owner can add missing fields (from the Bybit contract Info page)
via the API/dashboard. A SymbolSpec is only built when every REQUIRED field is present.
"""
from __future__ import annotations

import json
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ..config import PROJECT_ROOT
from ..db import database as db
from ..schemas.market import DataSource, SymbolSpec

REQUIRED = ("contract_size", "volume_min", "volume_step", "volume_max", "tick_size", "digits",
            "commission_per_lot", "margin_pct", "swap_type", "swap_long", "swap_short", "swap_triple_day",
            "profit_currency")
NUMERIC = {"contract_size", "volume_min", "volume_step", "volume_max", "tick_size", "digits", "commission_per_lot",
           "margin_pct", "swap_long", "swap_short"}
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY")


class SpecError(ValueError):
    pass


def load_seed(path: str | Path = "config/instruments.toml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    data.pop("sources", None)
    return data


def seed(conn: sqlite3.Connection, path: str | Path = "config/instruments.toml") -> list[str]:
    """Insert seed specs for symbols not yet in the DB. Never overwrites owner-entered values."""
    added = []
    for sym, d in load_seed(path).items():
        if conn.execute("SELECT 1 FROM instrument_specs WHERE symbol=?", (sym,)).fetchone():
            continue
        src = {k: "config/instruments.toml (see comment/source)" for k in d}
        conn.execute("INSERT INTO instrument_specs(symbol, data, field_sources, updated_utc, updated_by) VALUES (?,?,?,?,?)",
                     (sym, json.dumps(d), json.dumps(src), db.utc_now_iso(), "SYSTEM"))
        added.append(sym)
    if added:
        db.audit(conn, "SYSTEM", "SPECS_SEEDED", details={"symbols": added})
    return added


def get(conn: sqlite3.Connection, symbol: str) -> dict | None:
    r = conn.execute("SELECT * FROM instrument_specs WHERE symbol=?", (symbol,)).fetchone()
    if r is None:
        return None
    return {"symbol": symbol, "data": json.loads(r["data"]), "field_sources": json.loads(r["field_sources"]),
            "updated_utc": r["updated_utc"], "updated_by": r["updated_by"]}


def missing_fields(d: dict) -> list[str]:
    return [k for k in REQUIRED if d.get(k) in (None, "")]


def status(conn: sqlite3.Connection, symbol: str) -> dict:
    g = get(conn, symbol)
    if g is None:
        return {"symbol": symbol, "complete": False, "missing": list(REQUIRED)}
    miss = missing_fields(g["data"])
    return {"symbol": symbol, "complete": not miss, "missing": miss, "data": g["data"], "field_sources": g["field_sources"],
            "updated_utc": g["updated_utc"], "updated_by": g["updated_by"]}


def update(conn: sqlite3.Connection, symbol: str, fields: dict, source: str, actor: str) -> dict:
    if actor != "OWNER":
        raise SpecError("only the OWNER may change instrument specifications")
    if not source.strip():
        raise SpecError("a source is required (e.g. 'Bybit app XAUUSD+ Info, 2026-11-20')")
    g = get(conn, symbol) or {"data": {}, "field_sources": {}}
    clean = {}
    for k, v in fields.items():
        if k not in REQUIRED and k not in ("bybit_symbol", "asset_class", "provider_symbols"):
            raise SpecError(f"unknown field {k}")
        if k in NUMERIC:
            v = float(v)
            if k == "commission_per_lot" and v < 0:
                raise SpecError("commission_per_lot must be >= 0")
            if k not in ("swap_long", "swap_short", "commission_per_lot") and v <= 0:
                raise SpecError(f"{k} must be positive")
            if k == "digits":
                v = int(v)
        if k == "swap_triple_day" and str(v).upper() not in WEEKDAYS:
            raise SpecError("swap_triple_day must be a weekday name")
        if k == "swap_type" and str(v).upper() not in ("POINTS", "MONEY", "PERCENT"):
            raise SpecError("swap_type must be POINTS, MONEY or PERCENT")
        clean[k] = v.upper() if isinstance(v, str) and k in ("swap_triple_day", "swap_type", "profit_currency") else v
    data = {**g["data"], **clean}
    if data.get("volume_min") and data.get("volume_max") and data["volume_max"] < data["volume_min"]:
        raise SpecError("volume_max < volume_min")
    srcs = {**g["field_sources"], **{k: source for k in clean}}
    with db.transaction(conn):
        conn.execute("INSERT INTO instrument_specs(symbol, data, field_sources, updated_utc, updated_by) VALUES (?,?,?,?,?)"
                     " ON CONFLICT(symbol) DO UPDATE SET data=excluded.data, field_sources=excluded.field_sources,"
                     " updated_utc=excluded.updated_utc, updated_by=excluded.updated_by",
                     (symbol, json.dumps(data), json.dumps(srcs), db.utc_now_iso(), actor))
        db.audit(conn, actor, "SPEC_UPDATED", entity="instrument_specs", entity_id=symbol,
                 details={"fields": clean, "source": source})
    return status(conn, symbol)


def symbol_spec(conn: sqlite3.Connection, symbol: str, usd_per_profit_ccy: float | None = None) -> SymbolSpec | None:
    """Build a SymbolSpec only if complete. tick_value is DERIVED: contract_size * tick_size converted to USD.

    For non-USD profit currency a live conversion rate (USD per 1 unit of profit currency) is required.
    Returns None when anything is missing => callers must decide NO TRADE.
    """
    st = status(conn, symbol)
    if not st["complete"]:
        return None
    d = st["data"]
    pc = d["profit_currency"]
    if pc == "USD":
        conv = 1.0
    elif usd_per_profit_ccy:
        conv = usd_per_profit_ccy
    else:
        return None
    tick_value = d["contract_size"] * d["tick_size"] * conv
    return SymbolSpec(symbol=symbol, description=d.get("bybit_symbol", symbol), asset_class=d.get("asset_class", "OTHER"),
                      digits=int(d["digits"]), point=d["tick_size"], tick_size=d["tick_size"], tick_value=tick_value,
                      contract_size=d["contract_size"], volume_min=d["volume_min"], volume_max=d["volume_max"],
                      volume_step=d["volume_step"], margin_currency="USD", profit_currency=pc, stops_level_points=0,
                      swap_long=d["swap_long"], swap_short=d["swap_short"], commission_per_lot=d["commission_per_lot"],
                      trade_allowed=True, source=DataSource(name=f"spec:{st['updated_by']}@{st['updated_utc']}", is_mock=False),
                      fetched_utc=datetime.now(timezone.utc))

"""CLI: python -m tradedesk {init-db|status|verify-audit}"""
from __future__ import annotations

import argparse
import json
import sys

from .broker.status import broker_demo_status
from .config import get_settings, load_risk_limits
from .db import database as db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradedesk")
    ap.add_argument("command", choices=["init-db", "status", "verify-audit"])
    args = ap.parse_args(argv)
    s = get_settings()
    conn = db.init_db(s.db_path())
    if args.command == "init-db":
        print(json.dumps({"db": str(s.db_path()), "migrations": db.schema_version(conn)}, indent=2))
    elif args.command == "status":
        lim = load_risk_limits(s.tradedesk_risk_limits_file)
        print(json.dumps({"settings": s.public_dict(), "risk_limits_sha256": lim.sha256,
                          "broker_demo": broker_demo_status(s),
                          "kill_switch": db.get_state(conn, "kill_switch"),
                          "paused": db.get_state(conn, "paused")}, indent=2))
    else:
        ok, bad = db.verify_audit_chain(conn)
        print(json.dumps({"audit_chain_ok": ok, "first_bad_id": bad}))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

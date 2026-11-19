"""Local PAPER execution simulator. Not a broker. Never presented as BROKER_DEMO.

State lives in SQLite (orders, fills, positions, trades, system_state). Orders are
idempotent on client_order_id (derived from decision_id): a repeated submission
returns the existing order and records a DUPLICATE_ORDER_BLOCKED risk event.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from ..db import database as db
from ..risk.sizing import money_per_price_unit, risk_money
from ..schemas.market import AccountInfo, AccountTradeMode, Candle, DataSource, Quote, SymbolSpec

PAPER_SOURCE = DataSource(name="PAPER_SIMULATOR", is_mock=True)
M5 = timedelta(minutes=5)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


class PaperBroker:
    venue = "PAPER"

    def __init__(self, conn: sqlite3.Connection, initial_balance: float = 10_000.0, leverage: int = 100,
                 slippage_points: float = 5.0):
        self.conn = conn
        self.leverage = leverage
        self.slippage_points = slippage_points
        if db.get_state(conn, "paper_balance") is None:
            db.set_state(conn, "paper_balance", f"{initial_balance:.2f}", "SYSTEM")
            db.set_state(conn, "paper_initial_balance", f"{initial_balance:.2f}", "SYSTEM")

    # ---------- account ----------
    def balance(self) -> float:
        return float(db.get_state(self.conn, "paper_balance"))

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM positions WHERE venue='PAPER' AND status='OPEN'")]

    def unrealized(self, quotes: dict[str, Quote], specs: dict[str, SymbolSpec]) -> float:
        total = 0.0
        for p in self.open_positions():
            q, spec = quotes.get(p["symbol"]), specs.get(p["symbol"])
            if q is None or spec is None:
                continue
            mpu = money_per_price_unit(spec, p["volume_lots"])
            total += (q.bid - p["open_price"]) * mpu if p["side"] == "BUY" else (p["open_price"] - q.ask) * mpu
        return total

    def margin_used(self, quotes: dict[str, Quote], specs: dict[str, SymbolSpec]) -> float:
        from ..risk.sizing import required_margin
        m = 0.0
        for p in self.open_positions():
            spec = specs.get(p["symbol"])
            if spec:
                m += required_margin(spec, p["volume_lots"], p["open_price"], self.leverage)
        return m

    def account_info(self, quotes: dict[str, Quote], specs: dict[str, SymbolSpec], now: datetime) -> AccountInfo:
        bal = self.balance()
        eq = bal + self.unrealized(quotes, specs)
        mar = self.margin_used(quotes, specs)
        return AccountInfo(login="PAPER", server="LOCAL_PAPER_SIMULATOR", company="local", trade_mode=AccountTradeMode.PAPER,
                           currency="USD", balance=round(bal, 2), equity=round(eq, 2), margin=round(mar, 2),
                           margin_free=round(eq - mar, 2), leverage=self.leverage, trade_allowed=True,
                           source=PAPER_SOURCE, fetched_utc=now)

    def day_start_equity(self, now: datetime, equity: float) -> float:
        raw = db.get_state(self.conn, "paper_day_start")
        d = now.date().isoformat()
        if raw:
            obj = json.loads(raw)
            if obj.get("date") == d:
                return float(obj["equity"])
        db.set_state(self.conn, "paper_day_start", json.dumps({"date": d, "equity": round(equity, 2)}), "SYSTEM")
        return equity

    # ---------- orders ----------
    def place_order(self, *, decision_id: str, symbol: str, side: str, volume_lots: float, stop_loss: float,
                    take_profit: float | None, quote: Quote, spec: SymbolSpec, now: datetime) -> dict:
        coid = f"PAPER-{decision_id}"
        existing = self.conn.execute("SELECT * FROM orders WHERE client_order_id=?", (coid,)).fetchone()
        if existing:
            self.conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, decision_id, details)"
                              " VALUES (?,?,?,?,?)", (db.utc_now_iso(), "DUPLICATE_ORDER_BLOCKED", "WARN", decision_id,
                                                      json.dumps({"client_order_id": coid})))
            return {"duplicate": True, "order": dict(existing)}
        if stop_loss is None or volume_lots <= 0:
            raise ValueError("paper order requires stop-loss and positive volume")
        slip = self.slippage_points * spec.point
        price = quote.ask + slip if side == "BUY" else quote.bid - slip
        ts = _iso(now)
        pos_id = f"pos-{uuid.uuid4().hex[:12]}"
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO orders(client_order_id, decision_id, venue, created_utc, updated_utc, symbol, side,"
                " order_type, volume_lots, price, stop_loss, take_profit, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (coid, decision_id, "PAPER", ts, ts, symbol, side, "MARKET", volume_lots, price, stop_loss, take_profit,
                 "FILLED"))
            self.conn.execute(
                "INSERT INTO fills(fill_id, client_order_id, filled_utc, price, volume_lots, commission, slippage_points)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"fill-{uuid.uuid4().hex[:12]}", coid, ts, price, volume_lots,
                 (spec.commission_per_lot or 0) * volume_lots, self.slippage_points))
            self.conn.execute(
                "INSERT INTO positions(position_id, venue, symbol, side, volume_lots, open_price, stop_loss, take_profit,"
                " opened_utc, updated_utc, status, client_order_id, last_reconciled_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pos_id, "PAPER", symbol, side, volume_lots, price, stop_loss, take_profit, ts, ts, "OPEN", coid, ts))
            db.audit(self.conn, "EXECUTION", "PAPER_ORDER_FILLED", entity="orders", entity_id=coid,
                     details={"side": side, "lots": volume_lots, "price": price, "sl": stop_loss, "tp": take_profit})
        return {"duplicate": False, "client_order_id": coid, "position_id": pos_id, "price": price}

    def _close(self, p: dict, exit_price: float, when: datetime, reason: str, spec: SymbolSpec) -> dict:
        mpu = money_per_price_unit(spec, p["volume_lots"])
        gross = (exit_price - p["open_price"]) * mpu if p["side"] == "BUY" else (p["open_price"] - exit_price) * mpu
        comm = 2 * (spec.commission_per_lot or 0) * p["volume_lots"]
        swap = 0.0  # swap unknown for mock spec; recorded as 0 and flagged in reports
        net = gross - comm + swap
        planned = risk_money(spec, p["volume_lots"], p["open_price"], p["stop_loss"])
        order = self.conn.execute("SELECT decision_id FROM orders WHERE client_order_id=?", (p["client_order_id"],)).fetchone()
        dec = self.conn.execute("SELECT strategy_id, strategy_version FROM decisions WHERE decision_id=?",
                                (order["decision_id"],)).fetchone()
        tid = f"trd-{uuid.uuid4().hex[:12]}"
        ts = _iso(when)
        with db.transaction(self.conn):
            self.conn.execute("UPDATE positions SET status='CLOSED', updated_utc=? WHERE position_id=?", (ts, p["position_id"]))
            self.conn.execute(
                "INSERT INTO trades(trade_id, position_id, decision_id, venue, symbol, side, strategy_id, strategy_version,"
                " opened_utc, closed_utc, entry_price, exit_price, volume_lots, gross_pnl, commission, swap, net_pnl,"
                " planned_risk_money, r_multiple, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, p["position_id"], order["decision_id"], "PAPER", p["symbol"], p["side"],
                 dec["strategy_id"] if dec else None, dec["strategy_version"] if dec else None, p["opened_utc"], ts,
                 p["open_price"], exit_price, p["volume_lots"], round(gross, 2), round(comm, 2), swap, round(net, 2),
                 round(planned, 2), round(net / planned, 4) if planned else 0.0, reason))
            bal = self.balance() + net
            self.conn.execute("UPDATE system_state SET value=?, updated_utc=?, updated_by='EXECUTION' WHERE key='paper_balance'",
                              (f"{bal:.2f}", db.utc_now_iso()))
            db.audit(self.conn, "EXECUTION", "PAPER_POSITION_CLOSED", entity="trades", entity_id=tid,
                     details={"reason": reason, "exit": exit_price, "net": round(net, 2)})
        return {"trade_id": tid, "net": round(net, 2), "reason": reason}

    def process_bars(self, symbol: str, m5: list[Candle], spec: SymbolSpec) -> list[dict]:
        """Apply SL/TP to open positions using closed M5 bars after the last processed time."""
        closed = []
        sp_default = 25 * spec.point
        slip = self.slippage_points * spec.point
        for p in [x for x in self.open_positions() if x["symbol"] == symbol]:
            since = datetime.fromisoformat(p["last_reconciled_utc"])
            last_t = since
            for b in m5:
                if b.open_time_utc < since:
                    continue
                sp = (b.spread_points * spec.point) if b.spread_points is not None else sp_default
                if p["side"] == "BUY":
                    hit_sl, hit_tp = b.low <= p["stop_loss"], p["take_profit"] is not None and b.high >= p["take_profit"]
                    px_sl = (min(b.open, p["stop_loss"]) if b.open_time_utc > since else p["stop_loss"]) - slip
                else:
                    hit_sl, hit_tp = b.high + sp >= p["stop_loss"], p["take_profit"] is not None and b.low + sp <= p["take_profit"]
                    px_sl = (max(b.open + sp, p["stop_loss"]) if b.open_time_utc > since else p["stop_loss"]) + slip
                if hit_sl:   # conservative: stop first when both inside a bar
                    closed.append(self._close(p, px_sl, b.open_time_utc + M5, "STOP_LOSS", spec))
                    break
                if hit_tp:
                    closed.append(self._close(p, p["take_profit"], b.open_time_utc + M5, "TAKE_PROFIT", spec))
                    break
                last_t = b.open_time_utc + M5
            else:
                self.conn.execute("UPDATE positions SET last_reconciled_utc=? WHERE position_id=?", (_iso(last_t), p["position_id"]))
        return closed

    def close_all(self, quotes: dict[str, Quote], specs: dict[str, SymbolSpec], now: datetime, reason: str) -> list[dict]:
        out = []
        for p in self.open_positions():
            q, spec = quotes.get(p["symbol"]), specs.get(p["symbol"])
            if q is None or spec is None:
                db.log_error(self.conn, "paper", "CRITICAL", f"cannot close {p['position_id']}: no quote/spec")
                continue
            px = q.bid if p["side"] == "BUY" else q.ask
            out.append(self._close(p, px, now, reason, spec))
        return out

    def reconcile(self) -> dict:
        issues = []
        for p in self.open_positions():
            o = self.conn.execute("SELECT * FROM orders WHERE client_order_id=?", (p["client_order_id"],)).fetchone()
            f = self.conn.execute("SELECT COALESCE(SUM(volume_lots),0) v FROM fills WHERE client_order_id=?",
                                  (p["client_order_id"],)).fetchone()
            if o is None:
                issues.append({"position_id": p["position_id"], "issue": "no order"})
            elif abs(f["v"] - p["volume_lots"]) > 1e-9:
                issues.append({"position_id": p["position_id"], "issue": f"filled {f['v']} != position {p['volume_lots']}"})
            if p["stop_loss"] is None:
                issues.append({"position_id": p["position_id"], "issue": "no stop-loss"})
        dup = self.conn.execute(
            "SELECT o.decision_id, COUNT(*) n FROM positions p JOIN orders o ON o.client_order_id=p.client_order_id"
            " WHERE p.status='OPEN' GROUP BY o.decision_id HAVING n > 1").fetchall()
        issues += [{"decision_id": r["decision_id"], "issue": "duplicate open positions"} for r in dup]
        return {"ok": not issues, "issues": issues, "open_positions": len(self.open_positions())}

"""Internal PaperBroker. Virtual money only; nothing ever leaves this process.

Accounting model (mirrors the mechanics Bybit documents for its MT5 CFD accounts, without claiming identity):
- Balance = SUM(account_ledger.amount). The ledger is append-only (DB triggers).
- Commission = lots x commission_per_lot, charged from the balance when the position OPENS.
- Margin = exposure x margin_pct, exposure = price x lots x contract_size (converted to USD).
- Swap accrues to the open position (affects equity at once) when held past 00:00 server time
  (GMT+2 / GMT+3 during US-rule DST), tripled on the instrument's swap_triple_day; it is booked to the
  balance when the position closes.
- Equity = balance + unrealized PnL + accrued swap. Free margin = equity - margin. Margin level = equity/margin.
- Stop-out (house rule, configurable; Bybit's own level is not confirmed): margin level <= stop_out => close all.
- Long positions open at ask and close at bid; shorts open at bid and close at ask. Slippage (points) is applied
  against the trader on market fills and stop exits. Gaps through a stop fill at the (worse) market price.
- Orders are idempotent on client_order_id = "PAPER-" + decision_id.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..db import database as db
from ..schemas.market import AccountInfo, AccountTradeMode, Candle, DataSource, Quote, SymbolSpec

PAPER_SOURCE = DataSource(name="INTERNAL_PAPER_BROKER", is_mock=False)
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")


class PaperError(RuntimeError):
    pass


@dataclass(frozen=True)
class Instrument:
    """Everything the broker needs for one symbol. Built only from a COMPLETE spec."""
    spec: SymbolSpec
    margin_pct: float
    swap_type: str
    swap_triple_day: str
    usd_per_profit_ccy: float      # 1.0 for USD-quoted symbols

    def value_per_price_unit(self, lots: float) -> float:
        return lots * self.spec.contract_size * self.usd_per_profit_ccy

    def exposure_usd(self, lots: float, price: float) -> float:
        return price * lots * self.spec.contract_size * self.usd_per_profit_ccy

    def margin(self, lots: float, price: float) -> float:
        return self.exposure_usd(lots, price) * self.margin_pct / 100.0

    def commission(self, lots: float) -> float:
        return lots * (self.spec.commission_per_lot or 0.0)

    def swap_per_night(self, side: str, lots: float, price: float) -> float:
        rate = self.spec.swap_long if side == "BUY" else self.spec.swap_short
        if rate is None:
            return 0.0
        if self.swap_type == "POINTS":
            return rate * self.spec.point * self.spec.contract_size * lots * self.usd_per_profit_ccy
        if self.swap_type == "MONEY":
            return rate * lots
        return self.exposure_usd(lots, price) * rate / 100.0 / 365.0


def server_offset_hours(t: datetime) -> int:
    """Bybit CFD server time: GMT+3 from the 2nd Sunday of March 02:00 UTC to the 1st Sunday of November 02:00 UTC,
    otherwise GMT+2 (source: Bybit Help Center 'How to Get Started With CFD Trading')."""
    y = t.year

    def nth_sunday(month: int, n: int) -> datetime:
        d = datetime(y, month, 1, 2, 0, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    return 3 if nth_sunday(3, 2) <= t < nth_sunday(11, 1) else 2


def server_midnights(t0: datetime, t1: datetime) -> list[datetime]:
    """UTC instants of server-time 00:00 in (t0, t1]."""
    out = []
    d = (t0 + timedelta(hours=server_offset_hours(t0))).date()
    for k in range(0, (t1 - t0).days + 3):
        day = d + timedelta(days=k)
        guess = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        inst = guess - timedelta(hours=server_offset_hours(guess - timedelta(hours=2)))
        if t0 < inst <= t1:
            out.append(inst)
    return out


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


class PaperBroker:
    venue = "PAPER"

    def __init__(self, conn: sqlite3.Connection, initial_balance: float = 100.0, slippage_points: float = 2.0,
                 stop_out_level_pct: float = 50.0):
        self.conn = conn
        self.initial_balance = initial_balance
        self.slippage_points = slippage_points
        self.stop_out_level_pct = stop_out_level_pct
        self.account_id = self._ensure_account()

    # ------------------------------------------------------------------ account
    def _ensure_account(self) -> str:
        r = self.conn.execute("SELECT account_id FROM paper_accounts WHERE status='ACTIVE'").fetchone()
        if r:
            return r["account_id"]
        return self._create_account("initial account")

    def _create_account(self, note: str) -> str:
        aid = f"paper-{uuid.uuid4().hex[:8]}"
        now = db.utc_now_iso()
        own = not self.conn.in_transaction
        if own:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO paper_accounts(account_id, created_utc, initial_balance, status, note)"
                              " VALUES (?,?,?,?,?)", (aid, now, self.initial_balance, "ACTIVE", note))
            self.conn.execute("INSERT INTO account_ledger(account_id, created_utc, kind, amount, note) VALUES (?,?,?,?,?)",
                              (aid, now, "DEPOSIT", self.initial_balance, "virtual funds"))
            db.audit(self.conn, "SYSTEM", "PAPER_ACCOUNT_CREATED", entity="paper_accounts", entity_id=aid,
                     details={"initial_balance": self.initial_balance, "note": note})
        except BaseException:
            if own:
                self.conn.execute("ROLLBACK")
            raise
        if own:
            self.conn.execute("COMMIT")
        return aid

    def reset(self, actor: str, reason: str, close_quotes: dict | None = None, instruments: dict | None = None,
              now: datetime | None = None) -> dict:
        """Archive the active account and open a fresh one with the initial balance.
        Open positions must be closed first (pass quotes to close them at market)."""
        if actor != "OWNER":
            raise PaperError("only the OWNER may reset the paper account")
        if not reason.strip():
            raise PaperError("a reason is required")
        if self.open_positions():
            if close_quotes is None:
                raise PaperError("open positions exist: close them first")
            self.close_all(close_quotes, instruments or {}, now or datetime.now(timezone.utc), "MANUAL")
            if self.open_positions():
                raise PaperError("could not close all positions (missing quotes); reset aborted")
        old = self.account_id
        with db.transaction(self.conn):
            self.conn.execute("UPDATE paper_accounts SET status='ARCHIVED', archived_utc=? WHERE account_id=?",
                              (db.utc_now_iso(), old))
            new = self._create_account(f"reset by owner: {reason}")
            db.audit(self.conn, actor, "PAPER_ACCOUNT_RESET", entity="paper_accounts", entity_id=new,
                     details={"archived": old, "reason": reason})
        self.account_id = new
        return {"archived": old, "new_account": new, "balance": self.balance()}

    def balance(self) -> float:
        r = self.conn.execute("SELECT COALESCE(SUM(amount),0) s FROM account_ledger WHERE account_id=?",
                              (self.account_id,)).fetchone()
        return round(r["s"], 10)

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM positions WHERE venue='PAPER' AND status='OPEN' AND account_id=? ORDER BY opened_utc",
            (self.account_id,))]

    @staticmethod
    def _close_price(p: dict, q: Quote) -> float:
        return q.bid if p["side"] == "BUY" else q.ask

    def unrealized(self, p: dict, q: Quote, inst: Instrument) -> float:
        px = self._close_price(p, q)
        diff = px - p["open_price"] if p["side"] == "BUY" else p["open_price"] - px
        return diff * inst.value_per_price_unit(p["volume_lots"])

    def snapshot(self, quotes: dict[str, Quote], instruments: dict[str, Instrument], now: datetime) -> dict:
        bal = self.balance()
        upnl = swap = margin = 0.0
        unpriced = []
        for p in self.open_positions():
            q, inst = quotes.get(p["symbol"]), instruments.get(p["symbol"])
            swap += p["swap_accrued"]
            if q is None or inst is None:
                unpriced.append(p["position_id"])
                continue
            upnl += self.unrealized(p, q, inst)
            margin += inst.margin(p["volume_lots"], p["open_price"])
        equity = bal + upnl + swap
        day = now.date().isoformat()
        key = f"day_start_equity:{self.account_id}:{day}"
        ds = db.get_state(self.conn, key)
        if ds is None:
            ds = f"{equity:.10f}"
            db.set_state(self.conn, key, ds, "SYSTEM")
        initial = self.conn.execute("SELECT initial_balance FROM paper_accounts WHERE account_id=?",
                                    (self.account_id,)).fetchone()["initial_balance"]
        iso = now.isocalendar()
        wkey = f"week_start_equity:{self.account_id}:{iso[0]}-W{iso[1]:02d}"
        ws = db.get_state(self.conn, wkey)
        if ws is None:
            ws = f"{equity:.10f}"
            db.set_state(self.conn, wkey, ws, "SYSTEM")
        trades_today = self.conn.execute("SELECT COUNT(*) FROM orders WHERE account_id=? AND substr(created_utc,1,10)=?",
                                         (self.account_id, day)).fetchone()[0]
        return {"account_id": self.account_id, "currency": "USD", "initial_balance": initial,
                "balance": round(bal, 4), "equity": round(equity, 4), "unrealized_pnl": round(upnl, 4),
                "swap_accrued": round(swap, 4), "margin": round(margin, 4), "free_margin": round(equity - margin, 4),
                "margin_level_pct": round(equity / margin * 100, 2) if margin > 0 else None,
                "day_start_equity": round(float(ds), 4), "daily_pnl": round(equity - float(ds), 4),
                "total_pnl": round(equity - initial, 4), "unpriced_positions": unpriced,
                "week_start_equity": round(float(ws), 4), "weekly_pnl": round(equity - float(ws), 4),
                "trades_today": trades_today,
                "open_positions": len(self.open_positions())}

    def account_info(self, quotes, instruments, now) -> AccountInfo:
        s = self.snapshot(quotes, instruments, now)
        return AccountInfo(login=self.account_id, server="INTERNAL_PAPER_BROKER", company="internal",
                           trade_mode=AccountTradeMode.PAPER, currency="USD", balance=s["balance"], equity=s["equity"],
                           margin=s["margin"], margin_free=s["free_margin"], leverage=1, trade_allowed=True,
                           source=PAPER_SOURCE, fetched_utc=now)

    # ------------------------------------------------------------------ orders
    def place_order(self, *, decision_id: str, symbol: str, side: str, volume_lots: float, stop_loss: float,
                    take_profit: float | None, quote: Quote, inst: Instrument, now: datetime,
                    max_quote_age_seconds: float = 10.0) -> dict:
        coid = f"PAPER-{decision_id}"
        existing = self.conn.execute("SELECT * FROM orders WHERE client_order_id=?", (coid,)).fetchone()
        if existing:
            self.conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, decision_id, details)"
                              " VALUES (?,?,?,?,?)", (db.utc_now_iso(), "DUPLICATE_ORDER_BLOCKED", "WARN", decision_id,
                                                      json.dumps({"client_order_id": coid})))
            return {"duplicate": True, "order": dict(existing)}
        spec = inst.spec
        if side not in ("BUY", "SELL"):
            raise PaperError("side must be BUY or SELL")
        if stop_loss is None:
            raise PaperError("stop-loss is mandatory")
        age = (now - quote.time_utc).total_seconds()
        if age > max_quote_age_seconds or age < -5:
            raise PaperError(f"quote is stale ({age:.1f}s)")
        if volume_lots < spec.volume_min - 1e-12 or volume_lots > spec.volume_max + 1e-12:
            raise PaperError(f"volume {volume_lots} outside [{spec.volume_min}, {spec.volume_max}]")
        steps = volume_lots / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            raise PaperError(f"volume {volume_lots} is not a multiple of step {spec.volume_step}")
        slip = self.slippage_points * spec.point
        price = quote.ask + slip if side == "BUY" else quote.bid - slip
        if (side == "BUY" and stop_loss >= price) or (side == "SELL" and stop_loss <= price):
            raise PaperError("stop-loss on wrong side of fill price")
        if self.open_positions_symbol(symbol):
            raise PaperError("position already open in symbol (no averaging / stacking)")
        quotes = {symbol: quote}
        snap = self.snapshot(quotes, {symbol: inst}, now)
        comm = inst.commission(volume_lots)
        req_margin = inst.margin(volume_lots, price)
        if snap["free_margin"] - comm < req_margin:
            raise PaperError(f"insufficient free margin: need {req_margin:.4f} + commission {comm:.4f}, "
                             f"free {snap['free_margin']:.4f}")
        planned_risk = abs(price - stop_loss) * inst.value_per_price_unit(volume_lots) + comm
        ts = _iso(now)
        pos_id = f"pos-{uuid.uuid4().hex[:12]}"
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO orders(client_order_id, decision_id, venue, created_utc, updated_utc, symbol, side, order_type,"
                " volume_lots, price, stop_loss, take_profit, status, account_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (coid, decision_id, "PAPER", ts, ts, symbol, side, "MARKET", volume_lots, price, stop_loss, take_profit,
                 "FILLED", self.account_id))
            self.conn.execute(
                "INSERT INTO fills(fill_id, client_order_id, filled_utc, price, volume_lots, commission, slippage_points)"
                " VALUES (?,?,?,?,?,?,?)", (f"fill-{uuid.uuid4().hex[:12]}", coid, ts, price, volume_lots, comm,
                                             self.slippage_points))
            self.conn.execute(
                "INSERT INTO positions(position_id, venue, symbol, side, volume_lots, open_price, stop_loss, take_profit,"
                " opened_utc, updated_utc, status, client_order_id, last_reconciled_utc, account_id, commission_paid,"
                " swap_accrued, last_swap_utc, planned_risk_money) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pos_id, "PAPER", symbol, side, volume_lots, price, stop_loss, take_profit, ts, ts, "OPEN", coid, ts,
                 self.account_id, comm, 0.0, ts, planned_risk))
            if comm:
                self.conn.execute("INSERT INTO account_ledger(account_id, created_utc, kind, amount, ref) VALUES (?,?,?,?,?)",
                                  (self.account_id, ts, "COMMISSION", -comm, pos_id))
            db.audit(self.conn, "EXECUTION", "PAPER_ORDER_FILLED", entity="orders", entity_id=coid,
                     details={"side": side, "lots": volume_lots, "price": price, "sl": stop_loss, "tp": take_profit,
                              "commission": comm, "margin": req_margin, "quote_time": quote.time_utc.isoformat()})
        return {"duplicate": False, "client_order_id": coid, "position_id": pos_id, "price": price,
                "commission": comm, "margin": req_margin, "planned_risk_money": planned_risk}

    def open_positions_symbol(self, symbol: str) -> list[dict]:
        return [p for p in self.open_positions() if p["symbol"] == symbol]

    def close_position(self, p: dict, exit_price: float, when: datetime, reason: str, inst: Instrument) -> dict:
        diff = exit_price - p["open_price"] if p["side"] == "BUY" else p["open_price"] - exit_price
        gross = diff * inst.value_per_price_unit(p["volume_lots"])
        comm = p["commission_paid"]
        swap = p["swap_accrued"]
        net = gross - comm + swap
        planned = p["planned_risk_money"] or (abs(p["open_price"] - p["stop_loss"]) * inst.value_per_price_unit(p["volume_lots"]) + comm)
        order = self.conn.execute("SELECT decision_id FROM orders WHERE client_order_id=?", (p["client_order_id"],)).fetchone()
        dec = self.conn.execute("SELECT strategy_id, strategy_version FROM decisions WHERE decision_id=?",
                                (order["decision_id"],)).fetchone()
        tid = f"trd-{uuid.uuid4().hex[:12]}"
        ts = _iso(when)
        with db.transaction(self.conn):
            n = self.conn.execute("UPDATE positions SET status='CLOSED', updated_utc=? WHERE position_id=? AND status='OPEN'",
                                  (ts, p["position_id"])).rowcount
            if n != 1:
                raise PaperError(f"position {p['position_id']} is not open")
            self.conn.execute(
                "INSERT INTO trades(trade_id, position_id, decision_id, venue, symbol, side, strategy_id, strategy_version,"
                " opened_utc, closed_utc, entry_price, exit_price, volume_lots, gross_pnl, commission, swap, net_pnl,"
                " planned_risk_money, r_multiple, exit_reason, account_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, p["position_id"], order["decision_id"], "PAPER", p["symbol"], p["side"],
                 dec["strategy_id"] if dec else None, dec["strategy_version"] if dec else None, p["opened_utc"], ts,
                 p["open_price"], exit_price, p["volume_lots"], round(gross, 6), round(comm, 6), round(swap, 6),
                 round(net, 6), round(planned, 6), round(net / planned, 4) if planned else 0.0, reason, self.account_id))
            self.conn.execute("INSERT INTO account_ledger(account_id, created_utc, kind, amount, ref) VALUES (?,?,?,?,?)",
                              (self.account_id, ts, "REALIZED_PNL", gross, tid))
            if swap:
                self.conn.execute("INSERT INTO account_ledger(account_id, created_utc, kind, amount, ref) VALUES (?,?,?,?,?)",
                                  (self.account_id, ts, "SWAP", swap, tid))
            db.audit(self.conn, "EXECUTION", "PAPER_POSITION_CLOSED", entity="trades", entity_id=tid,
                     details={"reason": reason, "exit": exit_price, "gross": round(gross, 6), "net": round(net, 6)})
        return {"trade_id": tid, "position_id": p["position_id"], "net": round(net, 6), "reason": reason,
                "exit_price": exit_price}

    def on_quote(self, symbol: str, q: Quote, inst: Instrument, now: datetime, max_quote_age_seconds: float = 10.0) -> list[dict]:
        """Check SL/TP against a fresh quote. Stale quotes are ignored (positions stay open, flagged by caller)."""
        age = (now - q.time_utc).total_seconds()
        if age > max_quote_age_seconds:
            return []
        out = []
        slip = self.slippage_points * inst.spec.point
        for p in self.open_positions_symbol(symbol):
            if p["side"] == "BUY":
                if q.bid <= p["stop_loss"]:
                    out.append(self.close_position(p, q.bid - slip, now, "STOP_LOSS", inst))
                elif p["take_profit"] is not None and q.bid >= p["take_profit"]:
                    out.append(self.close_position(p, p["take_profit"], now, "TAKE_PROFIT", inst))
            else:
                if q.ask >= p["stop_loss"]:
                    out.append(self.close_position(p, q.ask + slip, now, "STOP_LOSS", inst))
                elif p["take_profit"] is not None and q.ask <= p["take_profit"]:
                    out.append(self.close_position(p, p["take_profit"], now, "TAKE_PROFIT", inst))
        return out

    def on_bars(self, symbol: str, bars: list[Candle], inst: Instrument) -> list[dict]:
        """Catch SL/TP touched between quote polls using closed bid bars (+ bar spread for shorts).
        If both are touched in one bar, the stop is assumed first (conservative)."""
        out = []
        slip = self.slippage_points * inst.spec.point
        for p in self.open_positions_symbol(symbol):
            since = datetime.fromisoformat(p["last_reconciled_utc"])
            last = since
            for b in bars:
                bar_end = b.open_time_utc + timedelta(seconds=b.timeframe.seconds)
                if b.open_time_utc < since or not b.is_closed:
                    continue
                sp = b.spread_price if b.spread_price is not None else (b.spread_points or 0) * inst.spec.point
                if p["side"] == "BUY":
                    if b.low <= p["stop_loss"]:
                        px = (min(b.open, p["stop_loss"]) if b.open_time_utc > since else p["stop_loss"]) - slip
                        out.append(self.close_position(p, px, bar_end, "STOP_LOSS", inst))
                        break
                    if p["take_profit"] is not None and b.high >= p["take_profit"]:
                        out.append(self.close_position(p, p["take_profit"], bar_end, "TAKE_PROFIT", inst))
                        break
                else:
                    if b.high + sp >= p["stop_loss"]:
                        px = (max(b.open + sp, p["stop_loss"]) if b.open_time_utc > since else p["stop_loss"]) + slip
                        out.append(self.close_position(p, px, bar_end, "STOP_LOSS", inst))
                        break
                    if p["take_profit"] is not None and b.low + sp <= p["take_profit"]:
                        out.append(self.close_position(p, p["take_profit"], bar_end, "TAKE_PROFIT", inst))
                        break
                last = bar_end
            else:
                self.conn.execute("UPDATE positions SET last_reconciled_utc=? WHERE position_id=?", (_iso(last), p["position_id"]))
        return out

    def apply_swaps(self, instruments: dict[str, Instrument], now: datetime) -> list[dict]:
        out = []
        for p in self.open_positions():
            inst = instruments.get(p["symbol"])
            if inst is None:
                continue
            last = datetime.fromisoformat(p["last_swap_utc"] or p["opened_utc"])
            mids = server_midnights(last, now)
            if not mids:
                continue
            total = 0.0
            for m in mids:
                srv_day_ended = (m + timedelta(hours=server_offset_hours(m)) - timedelta(days=1)).weekday()
                mult = 3 if WEEKDAYS[srv_day_ended] == inst.swap_triple_day else 1
                total += mult * inst.swap_per_night(p["side"], p["volume_lots"], p["open_price"])
            self.conn.execute("UPDATE positions SET swap_accrued=swap_accrued+?, last_swap_utc=? WHERE position_id=?",
                              (total, _iso(mids[-1]), p["position_id"]))
            out.append({"position_id": p["position_id"], "nights": len(mids), "swap": round(total, 6)})
        return out

    def check_stop_out(self, quotes, instruments, now) -> list[dict]:
        s = self.snapshot(quotes, instruments, now)
        if s["margin_level_pct"] is not None and s["margin_level_pct"] <= self.stop_out_level_pct:
            self.conn.execute("INSERT INTO risk_events(created_utc, event_type, severity, details) VALUES (?,?,?,?)",
                              (db.utc_now_iso(), "STOP_OUT", "CRITICAL", json.dumps({"margin_level": s["margin_level_pct"]})))
            return self.close_all(quotes, instruments, now, "OTHER")
        return []

    def close_all(self, quotes: dict[str, Quote], instruments: dict[str, Instrument], now: datetime, reason: str) -> list[dict]:
        out = []
        for p in self.open_positions():
            q, inst = quotes.get(p["symbol"]), instruments.get(p["symbol"])
            if q is None or inst is None:
                db.log_error(self.conn, "paper", "CRITICAL", f"cannot close {p['position_id']}: no quote/spec")
                continue
            slip = self.slippage_points * inst.spec.point
            px = q.bid - slip if p["side"] == "BUY" else q.ask + slip
            out.append(self.close_position(p, px, now, reason, inst))
        return out

    def reconcile(self) -> dict:
        issues = []
        for p in self.open_positions():
            f = self.conn.execute("SELECT COALESCE(SUM(volume_lots),0) v FROM fills WHERE client_order_id=?",
                                  (p["client_order_id"],)).fetchone()
            if abs(f["v"] - p["volume_lots"]) > 1e-9:
                issues.append({"position_id": p["position_id"], "issue": f"filled {f['v']} != position {p['volume_lots']}"})
            if p["stop_loss"] is None:
                issues.append({"position_id": p["position_id"], "issue": "no stop-loss"})
        # ledger consistency: realized pnl rows == trades gross, commission rows == fills commission
        a = self.account_id
        tr = self.conn.execute("SELECT COALESCE(SUM(gross_pnl),0) g, COALESCE(SUM(swap),0) s FROM trades WHERE account_id=?",
                               (a,)).fetchone()
        lg = self.conn.execute("SELECT COALESCE(SUM(CASE WHEN kind='REALIZED_PNL' THEN amount END),0) g,"
                               " COALESCE(SUM(CASE WHEN kind='SWAP' THEN amount END),0) s,"
                               " COALESCE(SUM(CASE WHEN kind='COMMISSION' THEN amount END),0) c FROM account_ledger WHERE account_id=?",
                               (a,)).fetchone()
        fc = self.conn.execute("SELECT COALESCE(SUM(f.commission),0) c FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id"
                               " WHERE o.account_id=?", (a,)).fetchone()
        if abs(tr["g"] - lg["g"]) > 1e-4 or abs(tr["s"] - lg["s"]) > 1e-4:
            issues.append({"issue": "ledger PnL/swap does not match trades"})
        if abs(-lg["c"] - fc["c"]) > 1e-4:
            issues.append({"issue": "ledger commission does not match fills"})
        return {"ok": not issues, "issues": issues, "open_positions": len(self.open_positions()), "account_id": a}

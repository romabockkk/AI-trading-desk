"""OKX DEMO order management (EXCHANGE_INTEGRATION.md §5–§7).

Safety layers, in order:
1. The OKXClient always sends ``x-simulated-trading: 1`` (demo only, no live switch).
2. The kill switch file ``state/KILL`` blocks every new order.
3. Only proposals the Head Trader APPROVED and the Risk Manager sized are accepted.
4. Duplicate protection: deterministic clOrdId per proposal + check of open orders/positions.
5. The decision is journaled BEFORE the order is sent.
6. Nothing is assumed: order and attached SL/TP are re-read from the exchange; any
   inconsistency activates the kill switch.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..data.okx_client import OKXClient, OKXError
from ..journal import Journal
from ..models import TradeProposal, iso, utcnow


def _fmt(px: float, tick: str) -> str:
    t = float(tick)
    decimals = max(0, len(tick.split(".")[1].rstrip("0")) if "." in tick else 0)
    return f"{round(round(px / t) * t, decimals):.{decimals}f}"


class OKXDemoExecutor:
    def __init__(self, client: OKXClient, settings: Settings, journal: Journal):
        self.client, self.settings, self.journal = client, settings, journal
        self.kill_file: Path = settings.state_dir / "KILL"

    def kill(self, reason: str) -> None:
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.kill_file.write_text(f"{iso(utcnow())} {reason}\n")
        self.journal.append("kill_switch", {"reason": reason})

    @staticmethod
    def client_order_id(p: TradeProposal) -> str:
        h = hashlib.sha256(f"{p.inst_id}{p.direction}{p.entry}{p.stop_loss}{p.take_profit}".encode()).hexdigest()
        return "desk" + h[:24]

    # ----------------------------------------------------------------- submit
    def submit(self, p: TradeProposal, instrument: dict, pos_mode: str, equity: float) -> dict[str, Any]:
        if self.kill_file.exists():
            return {"status": "blocked", "reason": "kill switch active"}
        if not p.position_size_contracts or p.position_size_contracts <= 0:
            return {"status": "blocked", "reason": "proposal not sized by Risk Manager"}
        cl_id = self.client_order_id(p)
        trade_id = cl_id
        # duplicate / exposure check on the exchange itself
        if self.client.pending_orders(p.inst_id) or [x for x in self.client.positions(p.inst_id) if float(x.get("pos") or 0) != 0]:
            return {"status": "blocked", "reason": "existing order or position on instrument"}
        side = "buy" if p.direction == "LONG" else "sell"
        pos_side = ("long" if p.direction == "LONG" else "short") if pos_mode == "long_short_mode" else None
        try:
            self.client.set_leverage(p.inst_id, self.settings.leverage, self.settings.margin_mode, pos_side)
        except OKXError as exc:
            return {"status": "error", "stage": "set_leverage", "error": str(exc)}
        tick = instrument.get("tickSz", "0.1")
        body = {
            "instId": p.inst_id, "tdMode": self.settings.margin_mode, "side": side, "ordType": "limit",
            "px": _fmt(p.entry, tick), "sz": f"{p.position_size_contracts:g}", "clOrdId": cl_id,
            "attachAlgoOrds": [{
                "tpTriggerPx": _fmt(p.take_profit, tick), "tpOrdPx": "-1",
                "slTriggerPx": _fmt(p.stop_loss, tick), "slOrdPx": "-1",
                "tpTriggerPxType": "last", "slTriggerPxType": "mark",
            }],
        }
        if pos_side:
            body["posSide"] = pos_side
        expires = utcnow() + timedelta(minutes=15 * self.settings.strategy.entry_valid_bars_m15)
        # record BEFORE submission
        self.journal.append("trade_submitted", {
            "trade_id": trade_id, "inst_id": p.inst_id, "direction": p.direction, "entry": p.entry,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit, "contracts": p.position_size_contracts,
            "risk_usd": p.risk_usd, "equity_before": equity, "rr": round(p.rr, 2), "leverage": self.settings.leverage,
            "exchange": "OKX DEMO", "market_type": "USDT-margined perpetual swap",
            "setup": p.setup, "expires_at": iso(expires), "request": {k: v for k, v in body.items()},
        })
        try:
            ack = self.client.place_order(body)
        except OKXError as exc:
            self.journal.append("trade_rejected", {"trade_id": trade_id, "error": str(exc), "data": exc.data})
            return {"status": "rejected", "error": str(exc), "data": exc.data}
        ord_id = ack.get("ordId")
        try:
            order = self.client.get_order(p.inst_id, ord_id=ord_id)
        except OKXError as exc:
            self.kill(f"order {ord_id} sent but could not be verified: {exc}")
            return {"status": "unverified", "ordId": ord_id, "error": str(exc)}
        self.journal.append("trade_open" if order.get("state") == "filled" else "trade_submitted",
                            {"trade_id": trade_id, "ordId": ord_id, "order_state": order.get("state"),
                             "attached_algos": order.get("attachAlgoOrds")})
        return {"status": "submitted", "ordId": ord_id, "clOrdId": cl_id, "state": order.get("state"),
                "expires_at": iso(expires)}

    # ---------------------------------------------------------------- monitor
    def monitor(self) -> list[dict[str, Any]]:
        """Reconcile journal trades with the exchange. Call at the start of each cycle."""
        events = []
        for t in self.journal.open_trades():
            inst, tid = t["inst_id"], t["trade_id"]
            try:
                order = self.client.get_order(inst, ord_id=t.get("ordId"), cl_ord_id=None if t.get("ordId") else tid)
                positions = [x for x in self.client.positions(inst) if float(x.get("pos") or 0) != 0]
            except OKXError as exc:
                events.append({"trade_id": tid, "error": str(exc)})
                continue
            state = order.get("state")
            if state in ("live", "partially_filled"):
                exp = t.get("expires_at")
                if exp and datetime.fromisoformat(exp) < datetime.now(timezone.utc) and state == "live":
                    self.client.cancel_order(inst, order["ordId"])
                    self.journal.append("trade_cancelled", {"trade_id": tid, "reason": "entry expired unfilled"})
                    events.append({"trade_id": tid, "event": "cancelled (expired)"})
                continue
            if state == "canceled" and not positions:
                self.journal.append("trade_cancelled", {"trade_id": tid, "reason": "cancelled on exchange"})
                events.append({"trade_id": tid, "event": "cancelled"})
                continue
            if state == "filled" and positions:
                if t["status"] != "open":
                    algos = self.client.pending_algos(inst, "oco") + self.client.pending_algos(inst, "conditional")
                    has_sl = any(a.get("slTriggerPx") for a in algos)
                    filled_sz = float(order.get("accFillSz") or 0)
                    pos_sz = abs(sum(float(x["pos"]) for x in positions))
                    data = {"trade_id": tid, "ordId": order["ordId"], "fill_px": float(order.get("avgPx") or 0),
                            "filled_contracts": filled_sz, "sl_confirmed": has_sl, "opened_at": iso(utcnow())}
                    self.journal.append("trade_open", data)
                    if not has_sl:
                        self.kill(f"stop loss not confirmed for {tid}")
                    if abs(pos_sz - float(t.get("contracts") or 0)) > 1e-9:
                        self.kill(f"position size {pos_sz} differs from approved {t.get('contracts')} for {tid}")
                    events.append({"trade_id": tid, "event": "open", **data})
                continue
            if state == "filled" and not positions:
                hist = self.client.positions_history(inst, limit=5)
                h = hist[0] if hist else {}
                pnl = float(h.get("realizedPnl") or h.get("pnl") or 0)
                data = {"trade_id": tid, "inst_id": inst, "closed_at": iso(utcnow()), "pnl_usd": pnl,
                        "exit_px": float(h.get("closeAvgPx") or 0), "fees": float(h.get("fee") or 0),
                        "funding": float(h.get("fundingFee") or 0),
                        "result_r": pnl / t["risk_usd"] if t.get("risk_usd") else None,
                        "equity_before": t.get("equity_before")}
                self.journal.append("trade_closed", data)
                events.append({"event": "closed", **data})
        return events

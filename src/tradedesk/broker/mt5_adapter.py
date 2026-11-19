"""MetaTrader 5 adapter for a Bybit CFD **demo** account (or another MT5 demo the owner chooses later).

Status: NOT CONNECTED in this project so far. The MetaTrader5 package ships Windows wheels only
(verified on PyPI: 5.0.6231, win_amd64), and Bybit's CFD-demo help page reportedly does not offer
MT5 client/API/EA access at launch. This module is ready for a Windows host; it is exercised in tests
with a fake `MetaTrader5` module only — that proves the guard logic, not a broker connection.

Safety
- Every order-changing call fetches a fresh account_info() and passes it through
  safety.require_broker_order_allowed(): mode, owner flags, kill switch, trade_mode == DEMO,
  exact server allowlist, "demo" in server name. Any doubt => DemoGuardViolation, nothing sent.
- client_order_id is written into the MT5 `comment`; an existing position/order with the same
  comment blocks resubmission (duplicate protection across restarts).
- Never logs credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import Settings
from ..safety import require_broker_order_allowed
from ..schemas.market import AccountInfo, AccountTradeMode, Candle, DataSource, Quote, SymbolSpec, Timeframe
from .base import BrokerAdapter, BrokerError, BrokerUnavailable

_TRADE_MODE = {0: AccountTradeMode.DEMO, 1: AccountTradeMode.CONTEST, 2: AccountTradeMode.REAL}
RETCODE_DONE = 10009


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as e:
        raise BrokerUnavailable("MetaTrader5 package not installed (Windows-only)") from e
    return mt5


class MT5Adapter(BrokerAdapter):
    name = "MT5"

    def __init__(self, settings: Settings, state: Callable[[], tuple[bool, bool, bool]],
                 server_tz: str = "Etc/GMT-2", mt5_module=None, magic: int = 770_001):
        """state() -> (integration_tests_passed, kill_switch_on, paused)."""
        self.settings = settings
        self.state = state
        self.tz = ZoneInfo(server_tz)
        self.mt5 = mt5_module
        self.magic = magic
        self.connected = False

    # ------------------------------------------------------------ connection
    def connect(self) -> None:
        self.mt5 = self.mt5 or _load_mt5()
        s = self.settings
        kw = {}
        if s.mt5_terminal_path:
            kw["path"] = s.mt5_terminal_path
        if s.mt5_login:
            kw.update(login=int(s.mt5_login), password=s.mt5_password.get_secret_value(), server=s.mt5_server)
        if not self.mt5.initialize(**kw):
            raise BrokerUnavailable(f"MT5 initialize failed: {self.mt5.last_error()}")
        self.connected = True
        acc = self.account_info()
        if acc.trade_mode is not AccountTradeMode.DEMO:
            self.disconnect()
            raise BrokerError(f"connected account is {acc.trade_mode.value}, not DEMO: disconnected")

    def disconnect(self) -> None:
        if self.mt5 and self.connected:
            self.mt5.shutdown()
        self.connected = False

    def _need(self):
        if not self.connected:
            raise BrokerUnavailable("not connected")
        return self.mt5

    def _src(self) -> DataSource:
        return DataSource(name=f"MT5:{self.settings.mt5_server or 'unknown'}", is_mock=False)

    def _server_epoch_to_utc(self, epoch: int) -> datetime:
        # MT5 returns server-local wall time encoded as a UTC epoch.
        naive = datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)
        return naive.replace(tzinfo=self.tz).astimezone(timezone.utc)

    # ------------------------------------------------------------ read
    def account_info(self) -> AccountInfo:
        a = self._need().account_info()
        if a is None:
            raise BrokerError(f"account_info failed: {self.mt5.last_error()}")
        return AccountInfo(login=str(a.login), server=a.server, company=getattr(a, "company", ""),
                           trade_mode=_TRADE_MODE.get(a.trade_mode, AccountTradeMode.UNKNOWN), currency=a.currency,
                           balance=a.balance, equity=a.equity, margin=a.margin, margin_free=a.margin_free,
                           leverage=a.leverage, trade_allowed=bool(a.trade_allowed), source=self._src(),
                           fetched_utc=datetime.now(timezone.utc))

    def is_demo(self) -> bool:
        try:
            return self.account_info().trade_mode is AccountTradeMode.DEMO
        except BrokerError:
            return False

    def symbols(self) -> list[str]:
        return [s.name for s in (self._need().symbols_get() or [])]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        i = self._need().symbol_info(symbol)
        if i is None:
            raise BrokerError(f"symbol_info({symbol}) unavailable")
        return SymbolSpec(symbol=i.name, description=getattr(i, "description", ""), asset_class="OTHER", digits=i.digits,
                          point=i.point, tick_size=i.trade_tick_size, tick_value=i.trade_tick_value,
                          contract_size=i.trade_contract_size, volume_min=i.volume_min, volume_max=i.volume_max,
                          volume_step=i.volume_step, margin_currency=i.currency_margin, profit_currency=i.currency_profit,
                          stops_level_points=i.trade_stops_level, swap_long=i.swap_long, swap_short=i.swap_short,
                          commission_per_lot=None, trade_allowed=i.trade_mode != 0, source=self._src(),
                          fetched_utc=datetime.now(timezone.utc))

    def quote(self, symbol: str) -> Quote:
        t = self._need().symbol_info_tick(symbol)
        if t is None:
            raise BrokerError(f"no tick for {symbol}")
        return Quote(symbol=symbol, time_utc=self._server_epoch_to_utc(t.time), bid=t.bid, ask=t.ask, source=self._src())

    def candles(self, symbol: str, timeframe: Timeframe, start_utc: datetime, end_utc: datetime) -> list[Candle]:
        mt5 = self._need()
        tf = getattr(mt5, f"TIMEFRAME_{timeframe.value}")
        rates = mt5.copy_rates_range(symbol, tf, start_utc, end_utc)
        if rates is None:
            raise BrokerError(f"copy_rates_range failed: {mt5.last_error()}")
        now = datetime.now(timezone.utc)
        out = []
        for r in rates:
            t = self._server_epoch_to_utc(r["time"])
            out.append(Candle(symbol=symbol, timeframe=timeframe, open_time_utc=t, open=float(r["open"]), high=float(r["high"]),
                              low=float(r["low"]), close=float(r["close"]), tick_volume=float(r["tick_volume"]),
                              spread_points=float(r["spread"]),
                              is_closed=(t.timestamp() + timeframe.seconds) <= now.timestamp()))
        return out

    def positions(self) -> list[dict]:
        return [p._asdict() if hasattr(p, "_asdict") else dict(p.__dict__) for p in (self._need().positions_get() or [])]

    # ------------------------------------------------------------ guarded writes
    def _guard(self) -> None:
        tests_ok, kill, paused = self.state()
        require_broker_order_allowed(self.settings, self.account_info(), integration_tests_passed=tests_ok,
                                     kill_switch_on=kill, paused=paused)

    def _check(self, res) -> dict:
        if res is None:
            raise BrokerError(f"order_send returned None: {self.mt5.last_error()}")
        if res.retcode != RETCODE_DONE:
            raise BrokerError(f"broker rejected: retcode={res.retcode} comment={getattr(res, 'comment', '')}")
        return {"retcode": res.retcode, "order": getattr(res, "order", None), "deal": getattr(res, "deal", None),
                "price": getattr(res, "price", None), "volume": getattr(res, "volume", None)}

    def place_demo_order(self, *, client_order_id: str, symbol: str, side: str, volume_lots: float,
                         stop_loss: float, take_profit: float | None) -> dict:
        if stop_loss is None:
            raise BrokerError("stop-loss is mandatory")
        self._guard()
        mt5 = self.mt5
        comment = client_order_id[:31]
        if any(getattr(p, "comment", "") == comment for p in (mt5.positions_get(symbol=symbol) or [])) or \
           any(getattr(o, "comment", "") == comment for o in (mt5.orders_get(symbol=symbol) or [])):
            raise BrokerError(f"duplicate: {comment} already exists at broker")
        tick = mt5.symbol_info_tick(symbol)
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume_lots),
               "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
               "price": tick.ask if side == "BUY" else tick.bid, "sl": float(stop_loss),
               "tp": float(take_profit) if take_profit else 0.0, "deviation": 20, "magic": self.magic,
               "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        out = self._check(mt5.order_send(req))
        # confirm the position exists with the protective stop
        pos = [p for p in (mt5.positions_get(symbol=symbol) or []) if getattr(p, "comment", "") == comment]
        out["confirmed_position"] = bool(pos) and all(getattr(p, "sl", 0) for p in pos)
        if not out["confirmed_position"]:
            raise BrokerError("order accepted but position/stop-loss not confirmed — manual check required")
        return out

    def modify_demo_order(self, *, broker_ticket: str, stop_loss: float | None, take_profit: float | None) -> dict:
        if stop_loss is None:
            raise BrokerError("removing the stop-loss is not allowed")
        self._guard()
        mt5 = self.mt5
        return self._check(mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": int(broker_ticket),
                                           "sl": float(stop_loss), "tp": float(take_profit or 0.0)}))

    def close_demo_position(self, *, broker_ticket: str) -> dict:
        self._guard()
        mt5 = self.mt5
        p = next((x for x in (mt5.positions_get() or []) if str(x.ticket) == str(broker_ticket)), None)
        if p is None:
            raise BrokerError(f"position {broker_ticket} not found")
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        return self._check(mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                                           "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                                           "position": p.ticket, "price": tick.bid if buy else tick.ask, "deviation": 20,
                                           "magic": self.magic, "type_time": mt5.ORDER_TIME_GTC,
                                           "type_filling": mt5.ORDER_FILLING_IOC}))

    def reconcile(self, local_positions: list[dict]) -> dict:
        broker = {getattr(p, "comment", ""): p for p in (self._need().positions_get() or [])}
        issues = []
        for lp in local_positions:
            bp = broker.get(lp["client_order_id"][:31])
            if bp is None:
                issues.append({"client_order_id": lp["client_order_id"], "issue": "missing at broker"})
            elif abs(bp.volume - lp["volume_lots"]) > 1e-9:
                issues.append({"client_order_id": lp["client_order_id"], "issue": "volume mismatch"})
            elif not getattr(bp, "sl", 0):
                issues.append({"client_order_id": lp["client_order_id"], "issue": "no stop-loss at broker"})
        local_ids = {lp["client_order_id"][:31] for lp in local_positions}
        issues += [{"comment": c, "issue": "unknown position at broker"} for c in broker if c not in local_ids]
        return {"ok": not issues, "issues": issues}

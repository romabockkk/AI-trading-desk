"""BrokerAdapter interface. Implementations: MockAdapter (stage 3), MT5Adapter (stage 10).

Contract:
- Read methods return schema objects tagged with their DataSource.
- Every *_demo_* method MUST call safety.require_broker_order_allowed() with a
  freshly fetched AccountInfo immediately before sending anything.
- No method may silently succeed: on failure raise BrokerError.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..schemas.market import AccountInfo, Candle, Quote, SymbolSpec, Timeframe


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


class BrokerAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abstractmethod
    def is_demo(self) -> bool: ...

    @abstractmethod
    def symbols(self) -> list[str]: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def candles(self, symbol: str, timeframe: Timeframe, start_utc: datetime, end_utc: datetime) -> list[Candle]: ...

    @abstractmethod
    def positions(self) -> list[dict]: ...

    @abstractmethod
    def place_demo_order(self, *, client_order_id: str, symbol: str, side: str, volume_lots: float,
                         stop_loss: float, take_profit: float | None) -> dict: ...

    @abstractmethod
    def modify_demo_order(self, *, broker_ticket: str, stop_loss: float | None,
                          take_profit: float | None) -> dict: ...

    @abstractmethod
    def close_demo_position(self, *, broker_ticket: str) -> dict: ...

    @abstractmethod
    def reconcile(self, local_positions: list[dict]) -> dict: ...

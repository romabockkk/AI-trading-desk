"""Market data schemas. Every object carries its source and whether it is mock data."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import UtcDatetime


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def seconds(self) -> int:
        return {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}[self.value]


class DataSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str                      # e.g. "MOCK", "MT5_DEMO:Bybit-Demo", "CSV:xauusd_m5.csv"
    is_mock: bool


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    timeframe: Timeframe
    open_time_utc: UtcDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: float = Field(ge=0)
    spread_points: float | None = Field(default=None, ge=0)
    is_closed: bool

    @model_validator(mode="after")
    def _ohlc(self) -> "Candle":
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close) or self.low > self.high:
            raise ValueError(f"inconsistent OHLC: o={self.open} h={self.high} l={self.low} c={self.close}")
        return self


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    time_utc: UtcDatetime
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    source: DataSource

    @model_validator(mode="after")
    def _ba(self) -> "Quote":
        if self.ask < self.bid:
            raise ValueError("ask < bid")
        return self

    def spread_points(self, point: float) -> float:
        return round((self.ask - self.bid) / point, 6)


class SymbolSpec(BaseModel):
    """Contract specification. Values must come from the broker, never assumed."""
    model_config = ConfigDict(frozen=True)
    symbol: str                    # exact broker symbol, e.g. "XAUUSD+" (verify in Market Watch)
    description: str = ""
    asset_class: str               # METAL, FX, INDEX, ENERGY, STOCK, OTHER
    digits: int = Field(ge=0, le=8)
    point: float = Field(gt=0)
    tick_size: float = Field(gt=0)
    tick_value: float = Field(gt=0)            # account-currency value of one tick for 1 lot
    contract_size: float = Field(gt=0)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    margin_currency: str
    profit_currency: str
    stops_level_points: float = Field(default=0, ge=0)
    swap_long: float | None = None
    swap_short: float | None = None
    commission_per_lot: float | None = None
    trade_allowed: bool
    source: DataSource
    fetched_utc: UtcDatetime

    @model_validator(mode="after")
    def _vol(self) -> "SymbolSpec":
        if self.volume_max < self.volume_min:
            raise ValueError("volume_max < volume_min")
        return self


class AccountTradeMode(str, Enum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"
    UNKNOWN = "UNKNOWN"


class AccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    login: str
    server: str
    company: str = ""
    trade_mode: AccountTradeMode
    currency: str
    balance: float
    equity: float
    margin: float = Field(ge=0)
    margin_free: float
    leverage: int = Field(ge=1)
    trade_allowed: bool
    source: DataSource
    fetched_utc: UtcDatetime

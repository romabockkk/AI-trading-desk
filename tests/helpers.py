from datetime import datetime, timedelta, timezone

from tradedesk.config import Settings, load_risk_limits
from tradedesk.schemas.market import AccountInfo, AccountTradeMode, Candle, DataSource, Quote, Timeframe

UTC = timezone.utc
T0 = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)   # Wednesday, market open
MOCK = DataSource(name="MOCK", is_mock=True)
LIMITS = load_risk_limits("config/risk_limits.toml")


def settings(**kw):
    return Settings(_env_file=None, **kw)


def candle(i, o, h, l, c, tf=Timeframe.M15, t0=T0, spread=20):
    return Candle(symbol="XAUUSD", timeframe=tf, open_time_utc=t0 + timedelta(seconds=tf.seconds * i), open=o, high=h,
                  low=l, close=c, tick_volume=100, spread_points=spread, is_closed=True)


def series(closes, tf=Timeframe.M15, wick=0.5):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(candle(i, o, max(o, c) + wick, min(o, c) - wick, c, tf))
        prev = c
    return out


def quote(bid=2650.00, ask=2650.25, t=T0, mock=True):
    return Quote(symbol="XAUUSD", time_utc=t, bid=bid, ask=ask, source=DataSource(name="MOCK" if mock else "MT5", is_mock=mock))


def account(equity=10_000.0, margin=0.0, mode=AccountTradeMode.PAPER):
    return AccountInfo(login="PAPER", server="LOCAL", trade_mode=mode, currency="USD", balance=equity, equity=equity,
                       margin=margin, margin_free=equity - margin, leverage=100, trade_allowed=True,
                       source=DataSource(name="PAPER", is_mock=True), fetched_utc=T0)

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


# ---------------------------------------------------------------- new-architecture fixtures
from tradedesk.data.mock_feed import MockFeed as _MockFeed            # noqa: E402
from tradedesk.data.resample import resample as _resample            # noqa: E402
from tradedesk.marketdata.providers import MockProvider, ProviderQuote  # noqa: E402

FIXTURE_SRC = DataSource(name="TEST-FIXTURE (synthetic prices, tests only)", is_mock=False)


class FixtureProvider(MockProvider):
    """TEST ONLY: synthetic prices labelled as a non-mock fixture so that pipeline paths requiring
    'real' data can be exercised. Never used outside tests."""
    name = "FIXTURE"

    def __init__(self, seed=7, spread_scale=1.0):
        super().__init__(seed)
        self.spread_scale = spread_scale
        self.source = FIXTURE_SRC

    def quotes(self, symbols, now=None):
        out = {}
        for s, pq in super().quotes(symbols, now).items():
            q = pq.quote
            mid, half = (q.bid + q.ask) / 2, (q.ask - q.bid) / 2 * self.spread_scale
            out[s] = ProviderQuote(Quote(symbol=s, time_utc=q.time_utc, bid=round(mid - half, 5), ask=round(mid + half, 5),
                                         source=FIXTURE_SRC), True)
        return out


GOLD_TEST_SPEC = {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 50, "tick_size": 0.01, "digits": 2,
                  "swap_long": -50, "swap_short": 10, "swap_triple_day": "WEDNESDAY"}


def complete_gold_spec(conn, **override):
    from tradedesk.marketdata import specs
    specs.seed(conn)
    return specs.update(conn, "XAUUSD", {**GOLD_TEST_SPEC, **override}, "TEST FIXTURE values (not Bybit)", "OWNER")


def market(provider=None, symbols=("XAUUSD", "USDJPY"), max_age=10.0):
    from tradedesk.data.service import MarketService
    return MarketService([provider or FixtureProvider()], symbols, max_age)


def gold_instrument(commission=6.0, swap_long=-50.0, swap_short=10.0, margin_pct=0.2, volume_min=0.01, volume_step=0.01):
    """TEST FIXTURE instrument (round numbers; Bybit-confirmed parts: contract 100, $6/lot, 0.2% margin)."""
    from tradedesk.execution.paper import Instrument
    from tradedesk.schemas.market import SymbolSpec
    spec = SymbolSpec(symbol="XAUUSD", asset_class="METAL", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                      contract_size=100, volume_min=volume_min, volume_max=50, volume_step=volume_step,
                      margin_currency="USD", profit_currency="USD", swap_long=swap_long, swap_short=swap_short,
                      commission_per_lot=commission, trade_allowed=True, source=FIXTURE_SRC, fetched_utc=T0)
    return Instrument(spec, margin_pct, "POINTS", "WEDNESDAY", 1.0)


def acct(equity=100.0, margin=0.0, daily_pnl=0.0):
    return {"equity": equity, "balance": equity, "margin": margin, "free_margin": equity - margin,
            "daily_pnl": daily_pnl, "day_start_equity": equity - daily_pnl, "initial_balance": equity,
            "week_start_equity": equity, "weekly_pnl": 0.0, "trades_today": 0}

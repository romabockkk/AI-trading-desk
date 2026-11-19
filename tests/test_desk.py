"""Offline tests: no network, no credentials."""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trading_desk.agents.team import (AccountState, DeskContext, DevilsAdvocate, HeadTrader, NewsAnalyst,
                                      QuantAnalyst, RiskManager, StrategyResearcher, TechnicalAnalyst)
from trading_desk.analysis import smc
from trading_desk.backtest import engine
from trading_desk.config import RiskLimits, Settings
from trading_desk.data.market_data import MarketSnapshot, candles_to_frame, resample
from trading_desk.data.okx_client import OKXClient
from trading_desk.execution.okx_executor import OKXDemoExecutor
from trading_desk.journal import Journal
from trading_desk.memory import Memory
from trading_desk.models import Decision, Stance, TradeProposal
from trading_desk.protections import check_protections
from trading_desk.sizing import position_size
from trading_desk.workspace import Workspace

INST = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


def frame(n: int, start="2026-01-01", freq="15min", seed=1, drift=0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(drift, 0.3, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.05, 0.3, n)
    low = np.minimum(open_, close) - rng.uniform(0.05, 0.3, n)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1000.0}, index=idx)


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.state_dir = tmp_path / "state"
    s.workspace_dir = tmp_path / "ws"
    return s


# ------------------------------------------------------------------ OKX client
def test_signature_matches_okx_spec():
    c = OKXClient(api_key="k", secret="secret", passphrase="p")
    ts, path, body = "2020-12-08T09:08:57.715Z", "/api/v5/account/balance?ccy=BTC", ""
    expected = base64.b64encode(hmac.new(b"secret", f"{ts}GET{path}{body}".encode(), hashlib.sha256).digest()).decode()
    assert c._sign(ts, "GET", path, body) == expected


def test_private_requests_always_demo(monkeypatch):
    c = OKXClient(api_key="k", secret="s", passphrase="p")
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"code": "0", "data": [{}]}

    def fake(method, url, data=None, headers=None, timeout=None):
        seen.update(headers)
        return R()

    monkeypatch.setattr(c.session, "request", fake)
    c.balance()
    assert seen["x-simulated-trading"] == "1"
    assert seen["OK-ACCESS-TIMESTAMP"].endswith("Z")


def test_private_without_credentials_refuses():
    with pytest.raises(Exception, match="credentials"):
        OKXClient().balance()


def test_candles_to_frame_drops_unconfirmed_and_sorts():
    rows = [["3000", "1", "2", "0.5", "1.5", "1", "1", "10", "0"],
            ["2000", "1", "2", "0.5", "1.5", "1", "1", "10", "1"],
            ["1000", "1", "2", "0.5", "1.5", "1", "1", "10", "1"]]
    df = candles_to_frame(rows)
    assert len(df) == 2 and df.index.is_monotonic_increasing


def test_resample_drops_incomplete_bucket():
    m15 = frame(4 * 5 + 2)            # 5 full hours + 2 bars
    h1 = resample(m15, "1h")
    assert len(h1) == 5


# ------------------------------------------------------------------ SMC rules
def test_swing_points_simple():
    v = np.array([1, 2, 3, 2, 1, 2, 5, 2, 1, 0, 1])
    assert smc.swing_points(v, 2, "high") == [2, 6]
    assert smc.swing_points(v, 2, "low") == [4]


def test_h4_bias():
    up = pd.DataFrame({"high": [1, 3, 1, 2, 5, 2, 3, 7, 3, 4, 5],
                       "low": [0, 2, 0.5, 1, 4, 1.5, 2, 6, 2.5, 3, 4]}, dtype=float)
    up["close"] = up["high"] - 0.2
    assert smc.h4_bias(up, 1).direction == "BULLISH"
    down = -up[["low", "high", "close"]].rename(columns={"low": "high", "high": "low"})
    assert smc.h4_bias(down, 1).direction == "BEARISH"


def test_detect_setup_never_crashes_on_random_data(settings):
    m15 = frame(3000, drift=0.01)
    for end in range(1700, 3000, 97):
        w = m15.iloc[:end]
        s = smc.detect_setup(w.iloc[-400:], resample(w, "1h").iloc[-300:], resample(w, "4h").iloc[-150:],
                             settings.strategy)
        assert s.status in {"NONE", "FORMING", "READY", "STALE", "INVALID"}
        if s.status == "READY":
            sign = 1 if s.direction == "LONG" else -1
            assert (s.entry - s.stop_loss) * sign > 0
            assert abs(s.take_profit - s.entry) / abs(s.entry - s.stop_loss) >= settings.strategy.min_rr - 1e-9


# ------------------------------------------------------------------ risk
def test_sizing_respects_one_percent():
    r = position_size(100.0, 99.0, 10_000, 10_000, 1.0, 3, 1.0, 0.01, 0.01, 0.0005, 0.0002)
    assert r.ok and r.risk_usd <= 100.0 + 1e-9 and r.risk_usd > 95


def test_sizing_capped_by_margin_never_exceeds_risk():
    r = position_size(100.0, 99.9, 10_000, 10_000, 1.0, 3, 1.0, 0.01, 0.01, 0.0005, 0.0002)
    assert r.capped_by_margin and r.margin <= 0.9 * 10_000 + 1e-6 and r.risk_usd <= 100


def test_sizing_rejects_too_tight_stop():
    r = position_size(100.0, 99.995, 10_000, 10_000, 1.0, 3, 1.0, 0.01, 0.01, 0.0, 0.0)
    assert not r.ok and "too tight" in r.reason


def test_protections():
    lim = RiskLimits()
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    losses = [{"closed_at": now - timedelta(hours=h), "pnl_usd": -100, "equity_before": 10_000} for h in (5, 3)]
    st = check_protections(losses, now, lim)
    assert st.blocked and any("StoplossGuard" in r for r in st.reasons) and any("MaxDrawdown" in r for r in st.reasons)
    win = [{"closed_at": now - timedelta(minutes=10), "pnl_usd": 50, "equity_before": 10_000}]
    assert any("Cooldown" in r for r in check_protections(win, now, lim).reasons)
    assert not check_protections([], now, lim).blocked


def test_journal_detects_tampering(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append("trade_submitted", {"trade_id": "a", "pnl": 0})
    j.append("trade_closed", {"trade_id": "a", "pnl_usd": -50})
    assert j.verify()[0]
    text = (tmp_path / "j.jsonl").read_text().replace("-50", "50")
    (tmp_path / "j.jsonl").write_text(text)
    assert not j.verify()[0]


# ------------------------------------------------------------------ team logic
def _ctx(settings, tmp_path, equity=10_000, verified=False):
    m15 = frame(400)
    snap = MarketSnapshot("TEST-USDT-SWAP", m15, resample(m15, "1h"), resample(m15, "4h"), instrument=INST,
                          funding={"fundingRate": "0.0001"}, open_interest={"oiUsd": "1e9"})
    acct = AccountState(equity, equity, verified)
    return DeskContext(settings, snap, acct, Journal(tmp_path / "j.jsonl"), Memory(tmp_path / "m.jsonl"),
                       news={"announcements": [], "events": None})


def _run_team(ctx, proposal):
    ws = Workspace(ctx.settings.workspace_dir, "t", ctx.snapshot.inst_id)

    class FakeTech(TechnicalAnalyst):
        def run(self, ws, ctx):
            rep = super().run(ws, ctx)
            rep.proposal = proposal
            rep.stance = Stance.SUPPORT
            return rep

    FakeTech().run(ws, ctx)
    for a in (QuantAnalyst, NewsAnalyst, StrategyResearcher, DevilsAdvocate, RiskManager):
        a().run(ws, ctx)
    return ws, HeadTrader().decide(ws, ctx)


def _proposal(price, direction="LONG", rr=2.5, stop=0.5):
    sign = 1 if direction == "LONG" else -1
    return TradeProposal("TEST-USDT-SWAP", direction, price, price - sign * stop, price + sign * stop * rr,
                         {"strategy_version": "1.0", "levels": {}, "zone": {"type": "FVG"}}, ["test"])


def test_good_proposal_is_approved_and_sized(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    p = _proposal(ctx.snapshot.price)
    ws, (decision, why) = _run_team(ctx, p)
    assert decision == Decision.APPROVED_FOR_DEMO, why
    assert p.position_size_contracts and p.risk_usd <= 100


def test_low_rr_is_vetoed(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    ws, (decision, why) = _run_team(ctx, _proposal(ctx.snapshot.price, rr=1.5))
    assert decision == Decision.REJECTED and "Risk Manager veto" in why


def test_kill_switch_vetoes(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "KILL").write_text("test")
    _, (decision, why) = _run_team(ctx, _proposal(ctx.snapshot.price))
    assert decision == Decision.REJECTED and "KILL" in why


def test_open_position_blocks_second_trade(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    ctx.account.open_positions = [{"instId": "BTC-USDT-SWAP", "pos": "1"}]
    _, (decision, _) = _run_team(ctx, _proposal(ctx.snapshot.price))
    assert decision == Decision.REJECTED


def test_stale_data_is_not_enough_data(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    ctx.snapshot.problems = ["stale data"]
    _, (decision, _) = _run_team(ctx, _proposal(ctx.snapshot.price))
    assert decision == Decision.NOT_ENOUGH_DATA


def test_no_setup_is_no_trade(settings, tmp_path):
    ctx = _ctx(settings, tmp_path)
    _, (decision, _) = _run_team(ctx, None)
    assert decision in (Decision.REJECTED, Decision.WAIT_FOR_CONFIRMATION)


# ------------------------------------------------------------------ execution
def test_executor_blocked_by_kill_switch(settings, tmp_path):
    ex = OKXDemoExecutor(OKXClient(), settings, Journal(tmp_path / "j.jsonl"))
    ex.kill("test")
    p = _proposal(100.0)
    p.position_size_contracts = 1
    assert ex.submit(p, INST, "net_mode", 10_000)["status"] == "blocked"


def test_executor_requires_sizing(settings, tmp_path):
    ex = OKXDemoExecutor(OKXClient(), settings, Journal(tmp_path / "j.jsonl"))
    assert ex.submit(_proposal(100.0), INST, "net_mode", 10_000)["status"] == "blocked"


def test_executor_builds_demo_order_with_sl_tp(settings, tmp_path):
    sent = {}

    class FakeClient:
        def pending_orders(self, inst): return []
        def positions(self, inst=None): return []
        def set_leverage(self, *a): return []
        def place_order(self, body):
            sent.update(body)
            return {"ordId": "1"}
        def get_order(self, inst, ord_id=None, cl_ord_id=None): return {"state": "live", "ordId": "1"}

    j = Journal(tmp_path / "j.jsonl")
    ex = OKXDemoExecutor(FakeClient(), settings, j)
    p = _proposal(100.0)
    p.position_size_contracts, p.risk_usd = 2.0, 50.0
    res = ex.submit(p, INST, "net_mode", 10_000)
    assert res["status"] == "submitted"
    algo = sent["attachAlgoOrds"][0]
    assert sent["side"] == "buy" and sent["px"] == "100.0" and algo["slTriggerPx"] == "99.5" and algo["tpTriggerPx"] == "101.2"
    kinds = [e["kind"] for e in j.entries()]
    assert kinds[0] == "trade_submitted"        # journaled BEFORE the order was sent


# ------------------------------------------------------------------ backtest
def test_backtest_fills_and_stops_conservatively(settings, monkeypatch):
    m15 = frame(2200)
    fired = {"done": False}

    def fake_detect(w15, h1, h4, p):
        if not fired["done"] and len(w15) == 401 and w15.index[-1] >= m15.index[1700]:
            fired["done"] = True
            px = float(w15["close"].iloc[-1])
            return smc.Setup("READY", "LONG", px, px - 0.6, px + 1.5, {"zone": {"type": "FVG"}})
        return smc.Setup("NONE")

    monkeypatch.setattr(engine, "detect_setup", fake_detect)
    res = engine.run_backtest(m15, "TEST", INST, settings)
    assert res.signals == 1 and len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason in ("stop_loss", "take_profit")
    assert t.fees_usd > 0
    if t.exit_reason == "stop_loss":
        assert t.result_r < -0.9
    s = res.summary()
    assert s["all"]["trades"] == 1

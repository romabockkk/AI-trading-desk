import pytest
from pydantic import ValidationError

from tradedesk.config import (OWNER_DEMO_AUTH_PHRASE, Mode, RiskLimits, Settings, load_risk_limits)


def test_default_mode_is_paper_and_broker_demo_disabled():
    s = Settings(_env_file=None)
    assert s.mode is Mode.PAPER
    assert s.tradedesk_broker_demo_enabled is False
    assert s.owner_demo_authorized is False


@pytest.mark.parametrize("bad", ["LIVE", "live", "REAL", "PRODUCTION"])
def test_live_mode_is_rejected(bad):
    with pytest.raises(ValidationError, match="forbidden"):
        Settings(_env_file=None, tradedesk_mode=bad)


def test_unknown_mode_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tradedesk_mode="FUTURES")


def test_no_live_member_in_mode_enum():
    assert "LIVE" not in Mode.__members__


def test_owner_phrase_must_match_exactly():
    assert not Settings(_env_file=None, tradedesk_owner_demo_authorization="yes").owner_demo_authorized
    assert Settings(_env_file=None, tradedesk_owner_demo_authorization=OWNER_DEMO_AUTH_PHRASE).owner_demo_authorized


def test_public_dict_has_no_secrets():
    s = Settings(_env_file=None, mt5_password="hunter2", llm_api_key="sk-secret",
                 tradedesk_owner_demo_authorization=OWNER_DEMO_AUTH_PHRASE)
    dumped = repr(s.public_dict()) + repr(s)
    assert "hunter2" not in dumped and "sk-secret" not in dumped and OWNER_DEMO_AUTH_PHRASE not in dumped


def test_shipped_risk_limits_match_spec():
    lim = load_risk_limits("config/risk_limits.toml").limits
    assert lim.max_risk_per_trade_pct == 0.5
    assert lim.max_total_open_risk_pct == 1.0
    assert lim.max_daily_drawdown_pct == 2.0
    assert lim.max_concurrent_positions == 1
    assert lim.min_reward_risk == 2.0
    assert lim.max_consecutive_losses == 2
    assert lim.require_stop_loss is True


def test_risk_limits_are_frozen():
    lim = load_risk_limits("config/risk_limits.toml").limits
    with pytest.raises(ValidationError):
        lim.max_risk_per_trade_pct = 5.0


def _base(**kw):
    d = dict(max_risk_per_trade_pct=0.5, max_total_open_risk_pct=1.0, max_daily_drawdown_pct=2.0,
             max_concurrent_positions=1, min_reward_risk=2.0, max_consecutive_losses=2,
             require_stop_loss=True, max_spread_points=50, max_spread_to_stop_ratio=0.15,
             max_margin_usage_pct=10, min_margin_level_pct=1000, max_effective_leverage=5,
             max_quote_age_seconds=10, max_candle_age_seconds_m5=900)
    d.update(kw)
    return d


@pytest.mark.parametrize("override", [
    {"require_stop_loss": False},
    {"max_risk_per_trade_pct": 3.0},
    {"max_total_open_risk_pct": 0.2},          # below per-trade
    {"min_margin_level_pct": 100},             # too close to stop-out
    {"unknown_key": 1},
])
def test_unsafe_limits_rejected(override):
    with pytest.raises(ValidationError):
        RiskLimits(**_base(**override))


def test_limits_file_hash_changes_with_content(tmp_path):
    src = open(load_risk_limits("config/risk_limits.toml").source_path).read()
    p = tmp_path / "r.toml"
    p.write_text(src)
    h1 = load_risk_limits(p).sha256
    p.write_text(src.replace("max_concurrent_positions = 1", "max_concurrent_positions = 2"))
    assert load_risk_limits(p).sha256 != h1

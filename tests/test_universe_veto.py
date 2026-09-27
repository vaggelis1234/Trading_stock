from src.universe.earnings import UnavailableEarningsProvider, earnings_within_48h
from src.universe.veto import apply_hard_vetoes


def _ok(**overrides):
    values = {
        "daily_close": 110.0,
        "daily_ema200": 100.0,
        "rsi14": 50.0,
        "atr_percent": 0.02,
        "volume_ratio": 1.0,
        "adv20": 10_000_000.0,
        "earnings_within_48h": None,
    }
    values.update(overrides)
    return values


def test_passing_metrics_have_no_veto_reasons():
    result = apply_hard_vetoes(**_ok())
    assert result.status == "PASS"
    assert result.reasons == ()
    assert result.earnings_check == "unavailable"


def test_close_below_ema_is_blocked_trend():
    result = apply_hard_vetoes(**_ok(daily_close=99.0, daily_ema200=100.0))
    assert result.status == "BLOCKED_TREND"
    assert result.reasons == ("BLOCKED_TREND",)


def test_close_equal_to_ema_is_blocked_trend():
    result = apply_hard_vetoes(**_ok(daily_close=100.0, daily_ema200=100.0))
    assert result.status == "BLOCKED_TREND"


def test_rsi_just_below_range_is_blocked():
    result = apply_hard_vetoes(**_ok(rsi14=34.9))
    assert result.status == "BLOCKED_RSI"
    assert result.reasons == ("BLOCKED_RSI",)


def test_rsi_just_above_range_is_blocked():
    result = apply_hard_vetoes(**_ok(rsi14=75.1))
    assert result.status == "BLOCKED_RSI"
    assert result.reasons == ("BLOCKED_RSI",)


def test_rsi_bounds_pass():
    assert apply_hard_vetoes(**_ok(rsi14=35)).status == "PASS"
    assert apply_hard_vetoes(**_ok(rsi14=75)).status == "PASS"


def test_atr_percent_just_above_cap_is_blocked_volatility():
    result = apply_hard_vetoes(**_ok(atr_percent=0.0401))
    assert result.status == "BLOCKED_VOLATILITY"
    assert result.reasons == ("BLOCKED_VOLATILITY",)


def test_atr_percent_cap_passes():
    assert apply_hard_vetoes(**_ok(atr_percent=0.04)).status == "PASS"


def test_volume_ratio_just_below_floor_is_blocked_volume():
    result = apply_hard_vetoes(**_ok(volume_ratio=0.79))
    assert result.status == "BLOCKED_VOLUME"
    assert result.reasons == ("BLOCKED_VOLUME",)


def test_volume_ratio_floor_passes():
    assert apply_hard_vetoes(**_ok(volume_ratio=0.8)).status == "PASS"


def test_adv_just_below_floor_is_blocked_liquidity():
    result = apply_hard_vetoes(**_ok(adv20=4_999_999))
    assert result.status == "BLOCKED_LIQUIDITY"
    assert result.reasons == ("BLOCKED_LIQUIDITY",)


def test_adv_floor_passes():
    assert apply_hard_vetoes(**_ok(adv20=5_000_000)).status == "PASS"


def test_multiple_failures_keep_every_reason():
    result = apply_hard_vetoes(**_ok(rsi14=34.9, volume_ratio=0.79, adv20=4_999_999))
    assert result.status == "BLOCKED_RSI"
    assert result.reasons == ("BLOCKED_RSI", "BLOCKED_VOLUME", "BLOCKED_LIQUIDITY")


def test_missing_inputs_do_not_pass():
    result = apply_hard_vetoes(**_ok(daily_close=float("nan")))
    assert result.status == "BLOCKED_INSUFFICIENT_DATA"
    assert "BLOCKED_INSUFFICIENT_DATA" in result.reasons
    assert "BLOCKED_TREND" not in result.reasons


def test_known_veto_is_kept_alongside_missing_data():
    result = apply_hard_vetoes(**_ok(rsi14=90.0, daily_ema200=None))
    assert result.status == "BLOCKED_RSI"
    assert result.reasons == ("BLOCKED_RSI", "BLOCKED_INSUFFICIENT_DATA")


def test_earnings_within_48h_blocks_when_the_provider_says_so():
    result = apply_hard_vetoes(**_ok(earnings_within_48h=True))
    assert result.status == "BLOCKED_EARNINGS"
    assert result.reasons == ("BLOCKED_EARNINGS",)
    assert result.earnings_check == "within_48h"


def test_clear_earnings_check_does_not_block():
    result = apply_hard_vetoes(**_ok(earnings_within_48h=False))
    assert result.status == "PASS"
    assert result.earnings_check == "clear"


def test_unavailable_earnings_provider_does_not_invent_a_date():
    provider = UnavailableEarningsProvider()
    assert provider.available is False
    assert earnings_within_48h(provider, "AAPL", "2024-06-03T15:00:00Z") is None
    result = apply_hard_vetoes(**_ok(earnings_within_48h=None))
    assert result.status == "PASS"
    assert "BLOCKED_EARNINGS" not in result.reasons

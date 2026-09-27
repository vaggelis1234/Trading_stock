import math

import pytest

from src.universe.scoring import (
    ADV20_MIN,
    DEFAULT_WEIGHTS,
    apply_score,
    factor_scores,
    load_universe_weights,
)


def test_shipped_weights_match_the_universe_v1_split():
    weights = load_universe_weights()
    assert weights == pytest.approx(DEFAULT_WEIGHTS)
    assert weights["trend"] == pytest.approx(0.30)
    assert weights["momentum"] == pytest.approx(0.30)
    assert weights["volatility_risk"] == pytest.approx(0.20)
    assert weights["volume_liquidity"] == pytest.approx(0.20)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_factor_score_is_the_documented_weighted_scale():
    adv = ADV20_MIN * math.sqrt(20)
    scores = factor_scores(
        daily_close=110.0,
        daily_ema200=100.0,
        hourly_close=100.0,
        rsi14=55.0,
        macd_hist=0.0,
        atr_percent=0.02,
        volume_ratio=1.4,
        adv20=adv,
        weights=DEFAULT_WEIGHTS,
    )
    assert scores["trend_score"] == pytest.approx(10.0)
    assert scores["momentum_score"] == pytest.approx(5.0)
    assert scores["volatility_score"] == pytest.approx(5.0)
    assert scores["volume_liquidity_score"] == pytest.approx(5.0)
    assert scores["final_score"] == pytest.approx(6.5)
    assert 0.0 <= scores["final_score"] <= 10.0


def test_trend_distance_above_the_cap_stays_on_the_score_scale():
    scores = factor_scores(
        daily_close=150.0,
        daily_ema200=100.0,
        hourly_close=150.0,
        rsi14=35.0,
        macd_hist=-3.0,
        atr_percent=0.04,
        volume_ratio=0.8,
        adv20=ADV20_MIN,
        weights=DEFAULT_WEIGHTS,
    )
    assert scores["trend_score"] == pytest.approx(10.0)
    assert 0.0 <= scores["final_score"] <= 10.0


def test_blocked_row_is_not_scored():
    row = {
        "veto_status": "BLOCKED_TREND",
        "final_score": 9.0,
        "trend_score": 9.0,
        "daily_close": 90.0,
        "daily_ema200": 100.0,
        "hourly_close": 90.0,
        "rsi14": 50.0,
        "macd_hist": 0.0,
        "atr_percent": 0.01,
        "volume_ratio": 1.0,
        "adv20": 20_000_000.0,
    }
    scored = apply_score(row, DEFAULT_WEIGHTS)
    assert scored["final_score"] is None
    assert scored["trend_score"] is None
    assert scored["momentum_score"] is None
    assert scored["volatility_score"] is None
    assert scored["volume_liquidity_score"] is None


def test_weights_that_do_not_sum_to_one_are_rejected(tmp_path):
    path = tmp_path / "weights.yaml"
    path.write_text("trend: 0.5\nmomentum: 0.5\nvolatility_risk: 0.5\nvolume_liquidity: 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_universe_weights(str(path))

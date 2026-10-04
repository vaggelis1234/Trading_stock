import math

import pytest

from src.universe.scoring import (
    ADV20_MIN,
    DEFAULT_WEIGHTS,
    SCORE_FIELDS,
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


def _valid_row(**overrides):
    row = {
        "veto_status": "PASS",
        "daily_close": 110.0,
        "daily_ema200": 100.0,
        "hourly_close": 100.0,
        "rsi14": 55.0,
        "macd_hist": 0.0,
        "atr_percent": 0.02,
        "volume_ratio": 1.4,
        "adv20": ADV20_MIN * math.sqrt(20),
    }
    row.update(overrides)
    return row


def test_pass_and_blocked_rows_with_valid_metrics_receive_the_same_score():
    expected = factor_scores(
        daily_close=110.0,
        daily_ema200=100.0,
        hourly_close=100.0,
        rsi14=55.0,
        macd_hist=0.0,
        atr_percent=0.02,
        volume_ratio=1.4,
        adv20=ADV20_MIN * math.sqrt(20),
        weights=DEFAULT_WEIGHTS,
    )
    passed = apply_score(_valid_row(), DEFAULT_WEIGHTS)
    blocked = apply_score(
        _valid_row(veto_status="BLOCKED_VOLUME", veto_reasons=["BLOCKED_VOLUME"]),
        DEFAULT_WEIGHTS,
    )
    assert passed["final_score"] == pytest.approx(expected["final_score"])
    assert blocked["final_score"] == pytest.approx(expected["final_score"])
    assert blocked["final_score"] == pytest.approx(6.5)
    assert blocked["veto_status"] == "BLOCKED_VOLUME"
    assert blocked["veto_reasons"] == ["BLOCKED_VOLUME"]
    for field in SCORE_FIELDS:
        assert passed[field] == pytest.approx(blocked[field])


def test_missing_scoring_input_receives_no_score():
    row = _valid_row(
        veto_status="BLOCKED_INSUFFICIENT_DATA",
        rsi14=None,
        final_score=9.0,
        trend_score=9.0,
        momentum_score=9.0,
        volatility_score=9.0,
        volume_liquidity_score=9.0,
    )
    scored = apply_score(row, DEFAULT_WEIGHTS)
    assert scored["veto_status"] == "BLOCKED_INSUFFICIENT_DATA"
    for field in SCORE_FIELDS:
        assert scored[field] is None


def test_non_finite_or_unusable_scoring_input_receives_no_score():
    for overrides in (
        {"macd_hist": float("nan")},
        {"daily_ema200": 0.0},
        {"hourly_close": 0.0},
        {"atr_percent": -0.01},
        {"adv20": -1.0},
    ):
        scored = apply_score(_valid_row(**overrides), DEFAULT_WEIGHTS)
        assert scored["final_score"] is None


def test_weights_that_do_not_sum_to_one_are_rejected(tmp_path):
    path = tmp_path / "weights.yaml"
    path.write_text("trend: 0.5\nmomentum: 0.5\nvolatility_risk: 0.5\nvolume_liquidity: 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_universe_weights(str(path))

from src.ui.universe_view import (
    DEFAULT_HISTORY_RANGE,
    HISTORY_RANGES,
    history_start,
    metric_coverage,
    rank_movement,
    reason_text,
)


def _row(**overrides):
    base = {
        "scan_timestamp": "2024-06-03T18:00:00+00:00",
        "symbol": "AAPL",
        "hourly_close": 190.5,
        "daily_close": 190.0,
        "daily_ema200": 180.0,
        "rsi14": 55.0,
        "macd_hist": 0.12,
        "atr14": 2.0,
        "atr_percent": 0.02,
        "volume": 1000.0,
        "volume_ratio": 1.25,
        "adv20": 25_000_000.0,
        "veto_status": "PASS",
        "veto_reasons": [],
        "earnings_check": "unavailable",
        "trend_score": 4.0,
        "momentum_score": 5.0,
        "volatility_score": 5.0,
        "volume_liquidity_score": 6.0,
        "final_score": 4.9,
        "rank": 1,
    }
    base.update(overrides)
    return base


def test_rank_movement_labels():
    assert rank_movement(30, 50) == (20, "🟢 ↑20")
    assert rank_movement(50, 30) == (-20, "🔴 ↓20")
    assert rank_movement(30, 30) == (0, "—")
    assert rank_movement(12, None) == (None, "NEW")


def test_blocked_reasons_are_readable():
    assert reason_text("BLOCKED_VOLUME") == "VolumeRatio < 0.8"
    assert reason_text("BLOCKED_RSI") == "RSI outside allowed range"
    assert reason_text("BLOCKED_VOLATILITY") == "ATR% > 4%"
    assert reason_text("BLOCKED_LIQUIDITY") == "ADV20 < $5M"
    assert reason_text("BLOCKED_TREND") == "Price <= EMA200"
    assert reason_text("BLOCKED_EARNINGS") == "Earnings within 48h"


def test_history_ranges_default_to_24h():
    assert list(HISTORY_RANGES) == ["24H", "3D", "5D"]
    assert DEFAULT_HISTORY_RANGE == "24H"
    anchor = "2024-06-03T18:00:00+00:00"
    assert history_start(anchor, "24H") == "2024-06-02T18:00:00+00:00"
    assert history_start(anchor, "3D") == "2024-05-31T18:00:00+00:00"
    assert history_start(anchor, "5D") == "2024-05-29T18:00:00+00:00"


def test_coverage_expects_session_windows_not_closed_hours():
    anchor = "2024-06-03T17:02:00+00:00"
    rows = [
        _row(scan_timestamp="2024-06-03T15:00:00+00:00", rsi14=40.0),
        _row(scan_timestamp="2024-06-03T16:00:00+00:00", rsi14=50.0),
        _row(scan_timestamp=anchor, rsi14=60.0),
    ]
    coverage = metric_coverage(rows, "RSI", anchor, "24H")
    assert coverage["expected"] == [
        "2024-06-03T13:30:00+00:00",
        "2024-06-03T14:00:00+00:00",
        "2024-06-03T14:30:00+00:00",
        "2024-06-03T15:00:00+00:00",
        "2024-06-03T15:30:00+00:00",
        "2024-06-03T16:00:00+00:00",
    ]
    assert coverage["actual"] == [
        "2024-06-03T15:00:00+00:00",
        "2024-06-03T16:00:00+00:00",
    ]
    assert coverage["missing"] == [
        "2024-06-03T13:30:00+00:00",
        "2024-06-03T14:00:00+00:00",
        "2024-06-03T14:30:00+00:00",
        "2024-06-03T15:30:00+00:00",
    ]
    assert "2024-06-02T22:00:00+00:00" not in coverage["expected"]
    assert history_start(anchor, "24H") == "2024-06-02T17:00:00+00:00"


def test_metric_coverage_separates_missing_snapshots_from_null_values():
    anchor = "2024-06-03T18:00:00+00:00"
    rows = [
        _row(scan_timestamp="2024-06-03T16:00:00+00:00", rsi14=50.0, final_score=None),
        _row(scan_timestamp="2024-06-03T18:00:00+00:00", rsi14=60.0, final_score=7.0),
    ]
    rsi = metric_coverage(rows, "RSI", anchor, "24H")
    assert rsi["expected"] == [
        "2024-06-03T13:30:00+00:00",
        "2024-06-03T14:00:00+00:00",
        "2024-06-03T14:30:00+00:00",
        "2024-06-03T15:00:00+00:00",
        "2024-06-03T15:30:00+00:00",
        "2024-06-03T16:00:00+00:00",
        "2024-06-03T16:30:00+00:00",
        "2024-06-03T17:00:00+00:00",
    ]
    assert rsi["actual"] == ["2024-06-03T16:00:00+00:00"]
    assert rsi["missing"] == [
        "2024-06-03T13:30:00+00:00",
        "2024-06-03T14:00:00+00:00",
        "2024-06-03T14:30:00+00:00",
        "2024-06-03T15:00:00+00:00",
        "2024-06-03T15:30:00+00:00",
        "2024-06-03T16:30:00+00:00",
        "2024-06-03T17:00:00+00:00",
    ]
    assert rsi["null_metric"] == []

    score = metric_coverage(rows, "Score", anchor, "24H")
    assert score["missing"] == rsi["missing"]
    assert score["null_metric"] == ["2024-06-03T16:00:00+00:00"]
    assert len(metric_coverage(rows, "Volume Ratio", anchor, "3D")["expected"]) == 12
    assert len(metric_coverage(rows, "ATR%", anchor, "5D")["expected"]) == 38

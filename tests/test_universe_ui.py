import pandas as pd

from src.ui.universe_view import DISPLAY_COLUMNS, history_chart_frame, latest_table


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


def test_latest_table_columns_and_blocked_rank():
    rows = [
        _row(),
        _row(
            symbol="XYZ",
            veto_status="BLOCKED_LIQUIDITY",
            veto_reasons=["BLOCKED_LIQUIDITY"],
            final_score=None,
            rank=None,
            atr_percent=0.0401,
            adv20=4_999_999,
        ),
    ]
    table = latest_table(rows)
    assert list(table.columns) == DISPLAY_COLUMNS
    assert int(table.iloc[0]["Rank"]) == 1
    assert table.iloc[0]["Symbol"] == "AAPL"
    assert table.iloc[0]["ATR %"] == 2.0
    assert pd.isna(table.iloc[1]["Rank"])
    assert pd.isna(table.iloc[1]["Score"])
    assert table.iloc[1]["Status"] == "BLOCKED_LIQUIDITY"
    assert table.iloc[1]["ATR %"] == 4.01


def test_history_chart_uses_snapshot_order_and_percent_points():
    rows = [
        _row(scan_timestamp="2024-06-03T17:00:00+00:00", final_score=4.0, atr_percent=0.01, rsi14=40, volume_ratio=0.9),
        _row(scan_timestamp="2024-06-03T18:00:00+00:00", final_score=None, atr_percent=0.02, rsi14=60, volume_ratio=1.1),
    ]
    chart = history_chart_frame(rows)
    assert list(chart.columns) == ["RSI", "Score", "ATR %", "Volume Ratio"]
    assert len(chart) == 2
    assert chart["RSI"].tolist() == [40, 60]
    assert chart["ATR %"].tolist() == [1.0, 2.0]
    assert pd.isna(chart["Score"].iloc[1])
    assert chart.index[0] < chart.index[1]

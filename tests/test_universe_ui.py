import pandas as pd

from src.ui.universe_view import (
    DEFAULT_HISTORY_RANGE,
    DISPLAY_COLUMNS,
    HISTORY_RANGES,
    format_local_timestamp,
    format_veto_reasons,
    history_chart_frame,
    history_start,
    history_status_table,
    latest_table,
    metric_coverage,
    metric_snapshot_options,
    rank_movement,
    reason_text,
    selected_ranking_symbol,
    symbol_detail_lines,
)
from src.universe.ranking import TOP_N


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


def test_latest_table_ranks_blocked_symbols_and_hides_unscored_rows():
    rows = [
        _row(symbol="AAPL", rank=2, final_score=8.0, veto_status="PASS", veto_reasons=[]),
        _row(
            symbol="XYZ",
            rank=1,
            final_score=9.1,
            veto_status="BLOCKED_LIQUIDITY",
            veto_reasons=["BLOCKED_LIQUIDITY"],
            atr_percent=0.0401,
            adv20=4_999_999,
        ),
        _row(
            symbol="NODATA",
            rank=None,
            final_score=None,
            veto_status="BLOCKED_INSUFFICIENT_DATA",
            veto_reasons=["BLOCKED_INSUFFICIENT_DATA"],
        ),
    ]
    table = latest_table(rows, {"XYZ": 21, "AAPL": 2})
    assert list(table.columns) == DISPLAY_COLUMNS
    assert table["Symbol"].tolist() == ["XYZ", "AAPL"]
    assert int(table.iloc[0]["Rank"]) == 1
    assert table.iloc[0]["Status"] == "BLOCKED"
    assert table.iloc[0]["Movement"] == "🟢 ↑20"
    assert table.iloc[0]["ATR%"] == 4.01
    assert table.iloc[1]["Status"] == "PASS"
    assert table.iloc[1]["Movement"] == "—"
    assert "BLOCKED_LIQUIDITY" not in table["Status"].tolist()


def test_latest_table_top_100_includes_blocked_and_excludes_rank_101():
    rows = []
    for rank in range(1, TOP_N + 2):
        status = "BLOCKED_VOLUME" if rank == 1 else "PASS"
        reasons = ["BLOCKED_VOLUME"] if rank == 1 else []
        rows.append(
            _row(
                symbol=f"S{rank:03d}",
                rank=rank,
                final_score=float(TOP_N + 2 - rank),
                veto_status=status,
                veto_reasons=reasons,
            )
        )
    table = latest_table(rows, {})
    assert len(table) == TOP_N
    assert table.iloc[0]["Symbol"] == "S001"
    assert table.iloc[0]["Status"] == "BLOCKED"
    assert table.iloc[0]["Movement"] == "NEW"
    assert "S101" not in set(table["Symbol"])


def test_pass_reason_is_dash_and_blocked_reasons_are_readable():
    assert format_veto_reasons("PASS", []) == "—"
    assert format_veto_reasons("PASS", ["BLOCKED_VOLUME"]) == "—"
    assert reason_text("BLOCKED_VOLUME") == "VolumeRatio < 0.8"
    assert reason_text("BLOCKED_RSI") == "RSI outside allowed range"
    assert reason_text("BLOCKED_VOLATILITY") == "ATR% > 4%"
    assert reason_text("BLOCKED_LIQUIDITY") == "ADV20 < $5M"
    assert reason_text("BLOCKED_TREND") == "Price <= EMA200"
    assert reason_text("BLOCKED_EARNINGS") == "Earnings within 48h"
    combined = format_veto_reasons(
        "BLOCKED_RSI",
        ["BLOCKED_RSI", "BLOCKED_VOLATILITY"],
    )
    assert combined == "RSI outside allowed range; ATR% > 4%"
    assert "BLOCKED_" not in combined


def test_symbol_detail_shows_rank_movement_and_blocked_reasons():
    blocked = _row(
        rank=8,
        final_score=8.91,
        veto_status="BLOCKED_VOLUME",
        veto_reasons=["BLOCKED_VOLUME", "BLOCKED_VOLATILITY"],
    )
    assert symbol_detail_lines(blocked, 11) == [
        "Current rank: 8",
        "Previous rank: 11",
        "Movement: 🟢 ↑3",
        "Score: 8.91",
        "Status: BLOCKED",
        "Reasons:",
        "• VolumeRatio < 0.8",
        "• ATR% > 4%",
    ]
    fresh = symbol_detail_lines(_row(rank=4, final_score=7.5, veto_status="PASS", veto_reasons=[]), None)
    assert fresh[:5] == [
        "Current rank: 4",
        "Previous rank: —",
        "Movement: NEW",
        "Score: 7.50",
        "Status: PASS",
    ]
    assert "Reasons:" not in fresh


def test_history_table_uses_pass_or_blocked_and_readable_reasons():
    rows = [
        _row(
            scan_timestamp="2024-06-03T17:30:00+00:00",
            veto_status="BLOCKED_VOLUME",
            veto_reasons=["BLOCKED_VOLUME"],
        ),
        _row(scan_timestamp="2024-06-03T19:30:00+00:00", veto_status="PASS", veto_reasons=[]),
        _row(
            scan_timestamp="2024-06-03T21:30:00+00:00",
            veto_status="BLOCKED_RSI",
            veto_reasons=["BLOCKED_RSI", "BLOCKED_VOLATILITY"],
        ),
    ]
    table = history_status_table(rows, with_date=False)
    assert table["Time"].tolist() == ["20:30", "22:30", "00:30"]
    assert table["Status"].tolist() == ["BLOCKED", "PASS", "BLOCKED"]
    assert table.iloc[0]["Reason"] == "VolumeRatio < 0.8"
    assert table.iloc[1]["Reason"] == "—"
    assert table.iloc[2]["Reason"] == "RSI outside allowed range; ATR% > 4%"


def test_history_ranges_default_to_24h():
    assert list(HISTORY_RANGES) == ["24H", "3D", "5D"]
    assert DEFAULT_HISTORY_RANGE == "24H"
    anchor = "2024-06-03T18:00:00+00:00"
    assert history_start(anchor, "24H") == "2024-06-02T18:00:00+00:00"
    assert history_start(anchor, "3D") == "2024-05-31T18:00:00+00:00"
    assert history_start(anchor, "5D") == "2024-05-29T18:00:00+00:00"


def test_history_chart_uses_snapshot_order_and_percent_points():
    rows = [
        _row(scan_timestamp="2024-06-03T17:00:00+00:00", final_score=4.0, atr_percent=0.01, rsi14=40, volume_ratio=0.9),
        _row(scan_timestamp="2024-06-03T18:00:00+00:00", final_score=None, atr_percent=0.02, rsi14=60, volume_ratio=1.1),
    ]
    chart = history_chart_frame(rows)
    assert list(chart.columns) == ["RSI", "Score", "ATR%", "Volume Ratio"]
    assert len(chart) == 2
    assert chart["RSI"].tolist() == [40, 60]
    assert chart["ATR%"].tolist() == [1.0, 2.0]
    assert pd.isna(chart["Score"].iloc[1])
    assert chart.index[0] < chart.index[1]
    assert str(chart.index.tz) == "Europe/Athens"


def test_ranking_row_selection_changes_symbol_detail_and_history():
    rows = [
        _row(symbol="AAA", rank=1, final_score=9.0, rsi14=70.0),
        _row(
            symbol="BBB",
            rank=2,
            final_score=4.0,
            rsi14=30.0,
            veto_status="BLOCKED_VOLUME",
            veto_reasons=["BLOCKED_VOLUME"],
        ),
    ]
    table = latest_table(rows, {})
    assert selected_ranking_symbol(table, {"selection": {"rows": []}}) == "AAA"
    selected = selected_ranking_symbol(table, {"selection": {"rows": [1]}})
    assert selected == "BBB"
    current = next(row for row in rows if row["symbol"] == selected)
    assert symbol_detail_lines(current, None)[4] == "Status: BLOCKED"
    history = history_status_table([current], with_date=False)
    assert history.iloc[0]["Status"] == "BLOCKED"
    assert history.iloc[0]["Reason"] == "VolumeRatio < 0.8"
    assert history_start(current["scan_timestamp"], "24H") == "2024-06-02T18:00:00+00:00"


def test_metric_dropdown_uses_athens_time_and_leaves_nulls_empty():
    rows = [
        _row(scan_timestamp="2024-06-03T19:30:00+00:00", rsi14=81.14, atr_percent=0.0125),
        _row(scan_timestamp="2024-01-15T18:00:00+00:00", rsi14=None, final_score=None, atr_percent=None),
    ]
    assert metric_snapshot_options(rows, "RSI") == [
        "03/06 22:30 — 81.14",
        "15/01 20:00 — —",
    ]
    assert metric_snapshot_options(rows, "ATR%") == [
        "03/06 22:30 — 1.25",
        "15/01 20:00 — —",
    ]
    assert "03/06 21:30" not in " ".join(metric_snapshot_options(rows, "Score"))


def test_metric_coverage_separates_missing_snapshots_from_null_values():
    anchor = "2024-06-03T18:00:00+00:00"
    rows = [
        _row(scan_timestamp="2024-06-03T16:00:00+00:00", rsi14=50.0, final_score=None),
        _row(scan_timestamp="2024-06-03T18:00:00+00:00", rsi14=60.0, final_score=7.0),
    ]
    rsi = metric_coverage(rows, "RSI", anchor, "24H")
    assert rsi["expected"][0] == "2024-06-02T18:00:00+00:00"
    assert rsi["expected"][-1] == anchor
    assert len(rsi["expected"]) == 25
    assert rsi["actual"] == [
        "2024-06-03T16:00:00+00:00",
        "2024-06-03T18:00:00+00:00",
    ]
    assert "2024-06-03T17:00:00+00:00" in rsi["missing"]
    assert "2024-06-03T16:00:00+00:00" not in rsi["missing"]
    assert rsi["null_metric"] == []

    score = metric_coverage(rows, "Score", anchor, "24H")
    assert score["missing"] == rsi["missing"]
    assert score["null_metric"] == ["2024-06-03T16:00:00+00:00"]
    assert len(metric_coverage(rows, "Volume Ratio", anchor, "3D")["expected"]) == 73
    assert len(metric_coverage(rows, "ATR%", anchor, "5D")["expected"]) == 121


def test_timestamps_display_in_europe_athens():
    summer = "2024-06-03T18:00:00+00:00"
    winter = "2024-01-15T18:00:00+00:00"
    assert format_local_timestamp(summer) == "2024-06-03 21:00 EEST"
    assert format_local_timestamp(winter) == "2024-01-15 20:00 EET"
    table = history_status_table([_row(scan_timestamp=summer)], with_date=False)
    assert table.iloc[0]["Time"] == "21:00"
    assert format_local_timestamp(None) == "—"

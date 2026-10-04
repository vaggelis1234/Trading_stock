import pandas as pd

from src.data.session_bars import (
    AFTER_HOURS,
    EXPECTED_MINUTES,
    MIXED,
    PRE_MARKET,
    REGULAR,
    classify_hourly_session,
)

_EXPECTED = {
    **{hour: PRE_MARKET for hour in range(4, 9)},
    9: MIXED,
    **{hour: REGULAR for hour in range(10, 16)},
    16: MIXED,
    **{hour: AFTER_HOURS for hour in range(17, 20)},
}


def _et(day: str, hour: int) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hour:02d}:00", tz="America/New_York")


def _minutes(stamps) -> pd.DataFrame:
    index = pd.DatetimeIndex(stamps)
    return pd.DataFrame({"close": 1.0}, index=index)


def test_est_and_edt_hours_share_the_same_session_type():
    est_days = ("2026-03-06", "2025-11-03")
    edt_days = ("2026-03-09", "2025-10-31")
    for hour, session_type in _EXPECTED.items():
        labels = []
        utc_hours = []
        for day in (*est_days, *edt_days):
            stamp = _et(day, hour)
            result = classify_hourly_session(stamp)
            utc_result = classify_hourly_session(stamp.tz_convert("UTC"))
            assert result.valid is True
            assert result.session_type == session_type
            assert utc_result.session_type == session_type
            assert result.expected_minutes == EXPECTED_MINUTES
            assert result.observed_minutes is None
            assert result.coverage_ratio is None
            labels.append(result.session_type)
            utc_hours.append(stamp.tz_convert("UTC").hour)
        assert len(set(labels)) == 1
        assert len(set(utc_hours)) == 2


def test_timestamps_outside_the_grid_are_invalid():
    outside = (
        _et("2026-03-09", 3),
        _et("2026-03-09", 20),
        pd.Timestamp("2026-03-09 00:00", tz="UTC"),
    )
    off_grid = (
        pd.Timestamp("2026-03-09 09:30", tz="America/New_York"),
        pd.Timestamp("2026-03-09 16:30", tz="America/New_York"),
    )
    for stamp in outside:
        result = classify_hourly_session(stamp)
        assert result.valid is False
        assert result.session_type is None
        assert result.reason == "outside_session"
    for stamp in off_grid:
        result = classify_hourly_session(stamp)
        assert result.valid is False
        assert result.session_type is None
        assert result.reason == "off_grid"
    naive = classify_hourly_session(pd.Timestamp("2026-03-09 10:00"))
    assert naive.valid is False
    assert naive.reason == "naive_timestamp"


def test_observed_minutes_count_distinct_bars_inside_the_hour():
    hour = _et("2026-03-09", 10)
    minutes = _minutes(
        [
            hour,
            hour,
            hour + pd.Timedelta(minutes=5),
            hour + pd.Timedelta(minutes=59),
            hour + pd.Timedelta(hours=1),
            hour - pd.Timedelta(minutes=1),
        ]
    )
    counted = classify_hourly_session(hour, minutes)
    assert counted.observed_minutes == 3
    assert counted.coverage_ratio == 3 / 60

    untouched = classify_hourly_session(hour, None)
    assert untouched.observed_minutes is None
    assert untouched.coverage_ratio is None

    empty = classify_hourly_session(hour, minutes.iloc[0:0])
    assert empty.observed_minutes == 0
    assert empty.coverage_ratio == 0.0

    naive_index = pd.DatetimeIndex([stamp.tz_localize(None) for stamp in minutes.index])
    skipped = classify_hourly_session(hour, pd.DataFrame({"close": 1.0}, index=naive_index))
    assert skipped.valid is True
    assert skipped.session_type == REGULAR
    assert skipped.observed_minutes is None
    assert skipped.reason == "naive_minute_index"

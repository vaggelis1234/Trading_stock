"""The 30-minute windows keep the same thresholds. This records that ATR% moves."""

from datetime import date

import pandas as pd

from scripts.compare_30m_scoring import compare_symbol, summarize
from src.market.calendar import NyseCalendar
from src.universe.veto import ATR_PERCENT_MAX


def _session_bars(day: date):
    calendar = NyseCalendar()
    index = []
    for session in calendar.recent_sessions(day, 80):
        cursor = pd.Timestamp(session.day, tz="America/New_York") + pd.Timedelta(hours=9, minutes=30)
        close_at = pd.Timestamp(session.day, tz="America/New_York") + pd.Timedelta(
            hours=session.close_et.hour, minutes=session.close_et.minute
        )
        while cursor + pd.Timedelta(minutes=30) <= close_at:
            index.append(cursor)
            cursor += pd.Timedelta(minutes=30)
    close = []
    price = 100.0
    for step in range(len(index)):
        price += 0.4 if step % 2 == 0 else -0.15
        close.append(price)
    frame = pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.2 for value in close],
            "low": [value - 1.2 for value in close],
            "close": close,
            "volume": [1000.0 + (step % 5) * 100 for step in range(len(index))],
        },
        index=pd.DatetimeIndex(index),
    )
    return frame


def test_half_hour_atr_is_not_treated_as_the_same_number_and_the_threshold_stays():
    day = date(2026, 10, 20)
    daily_index = pd.bdate_range("2025-01-02", periods=220, tz="America/New_York")
    daily_close = [80.0 + step * 0.2 for step in range(len(daily_index))]
    daily = pd.DataFrame(
        {
            "open": daily_close,
            "high": daily_close,
            "low": daily_close,
            "close": daily_close,
            "volume": 2_000_000.0,
        },
        index=daily_index,
    )
    as_of = pd.Timestamp("2026-10-20 16:15", tz="America/New_York")
    row = compare_symbol("AAPL", _session_bars(day), daily, as_of, day)
    summary = summarize([row])
    legacy = row["legacy_hourly"]["atr_percent"]
    current = row["half_hour"]["atr_percent"]
    assert legacy is not None and current is not None
    assert current < legacy
    assert summary["fields"]["atr_percent"]["half_over_legacy_median"] < 1
    assert summary["threshold_atr_percent"] == ATR_PERCENT_MAX == 0.04

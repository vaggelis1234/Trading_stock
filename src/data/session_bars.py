"""Hourly session labels and minute-coverage facts for the SIP clock grid.

The canonical hourly OHLCV bar stays the Alpaca SIP 1h bar. These helpers
only name that bar's New York hour and, when a 1-minute lookup was actually
run, count the distinct SIP minute bars inside it.

Session type is the America/New_York wall clock of the bar timestamp. The
UTC hour is never the classifier. A missing minute is a minute with no trade
bar. It is not a closed market and it is not a data-quality score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

PRE_MARKET = "PRE_MARKET"
MIXED = "MIXED"
REGULAR = "REGULAR"
AFTER_HOURS = "AFTER_HOURS"
EXPECTED_MINUTES = 60
NEW_YORK = "America/New_York"

_HOUR_SESSION = {
    **{hour: PRE_MARKET for hour in range(4, 9)},
    9: MIXED,
    **{hour: REGULAR for hour in range(10, 16)},
    16: MIXED,
    **{hour: AFTER_HOURS for hour in range(17, 20)},
}


@dataclass(frozen=True)
class HourlySession:
    """Classification of one hourly timestamp.

    ``valid`` is false when the timestamp is not an exact 04:00–19:00 ET
    clock label. ``observed_minutes`` is ``None`` when no 1-minute lookup
    was supplied. That is a different fact from a lookup that found zero bars.
    """

    valid: bool
    session_type: Optional[str]
    expected_minutes: Optional[int]
    observed_minutes: Optional[int]
    coverage_ratio: Optional[float]
    reason: Optional[str] = None


def classify_hourly_session(
    timestamp,
    minute_bars: Optional[pd.DataFrame] = None,
) -> HourlySession:
    """Label one hourly bar and, when ``minute_bars`` is a frame, count it.

    Pass ``minute_bars=None`` when the 1-minute feed was not read.
    Pass an empty frame when it was read and returned no bars.
    """
    et = _as_new_york(timestamp)
    if et is None:
        return _invalid("naive_timestamp")

    session_type = _session_type(et)
    if session_type is None:
        reason = "outside_session" if _outside_session(et) else "off_grid"
        return _invalid(reason)

    observed = _observed_minutes(et, minute_bars)
    if observed is None and minute_bars is not None and len(minute_bars) > 0 and _naive_index(minute_bars):
        return HourlySession(
            valid=True,
            session_type=session_type,
            expected_minutes=EXPECTED_MINUTES,
            observed_minutes=None,
            coverage_ratio=None,
            reason="naive_minute_index",
        )
    ratio = None if observed is None else observed / EXPECTED_MINUTES
    return HourlySession(
        valid=True,
        session_type=session_type,
        expected_minutes=EXPECTED_MINUTES,
        observed_minutes=observed,
        coverage_ratio=ratio,
    )


def _invalid(reason: str) -> HourlySession:
    return HourlySession(
        valid=False,
        session_type=None,
        expected_minutes=None,
        observed_minutes=None,
        coverage_ratio=None,
        reason=reason,
    )


def _as_new_york(value) -> Optional[pd.Timestamp]:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return None
    return stamp.tz_convert(NEW_YORK)


def _outside_session(et: pd.Timestamp) -> bool:
    wall = et.hour * 3600 + et.minute * 60 + et.second + et.microsecond / 1_000_000 + et.nanosecond / 1_000_000_000
    return wall < 4 * 3600 or wall >= 20 * 3600


def _session_type(et: pd.Timestamp) -> Optional[str]:
    if et.minute or et.second or et.microsecond or et.nanosecond:
        return None
    return _HOUR_SESSION.get(int(et.hour))


def _naive_index(minute_bars: pd.DataFrame) -> bool:
    index = minute_bars.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index))
    return index.tz is None


def _observed_minutes(et_hour: pd.Timestamp, minute_bars: Optional[pd.DataFrame]) -> Optional[int]:
    return _observed_in(et_hour, minute_bars, pd.Timedelta(hours=1))


HALF_HOUR_MINUTES = 30


def classify_half_hour_session(
    timestamp,
    minute_bars: Optional[pd.DataFrame] = None,
) -> HourlySession:
    """Label one 30-minute bar. ``:00`` and ``:30`` inside the SIP day are valid.

    A bar whose whole window is inside 09:30–16:00 New York is ``REGULAR``.
    The 09:00 bar is pre-market. The 16:00 bar is after-hours. Expected
    minute count is 30.
    """
    et = _as_new_york(timestamp)
    if et is None:
        return _invalid("naive_timestamp")
    if et.second or et.microsecond or et.nanosecond or int(et.minute) not in (0, 30):
        return _invalid("off_grid" if not _outside_session(et) else "outside_session")
    session_type = _half_hour_session_type(et)
    if session_type is None:
        reason = "outside_session" if _outside_session(et) else "off_grid"
        return _invalid(reason)
    observed = _observed_in(et, minute_bars, pd.Timedelta(minutes=HALF_HOUR_MINUTES))
    if observed is None and minute_bars is not None and len(minute_bars) > 0 and _naive_index(minute_bars):
        return HourlySession(
            valid=True,
            session_type=session_type,
            expected_minutes=HALF_HOUR_MINUTES,
            observed_minutes=None,
            coverage_ratio=None,
            reason="naive_minute_index",
        )
    ratio = None if observed is None else observed / HALF_HOUR_MINUTES
    return HourlySession(
        valid=True,
        session_type=session_type,
        expected_minutes=HALF_HOUR_MINUTES,
        observed_minutes=observed,
        coverage_ratio=ratio,
    )


def _half_hour_session_type(et: pd.Timestamp) -> Optional[str]:
    start = et.hour * 60 + et.minute
    end = start + HALF_HOUR_MINUTES
    if start < 4 * 60 or start >= 20 * 60:
        return None
    if end <= 9 * 60 + 30:
        return PRE_MARKET
    if start >= 16 * 60:
        return AFTER_HOURS
    if start >= 9 * 60 + 30 and end <= 16 * 60:
        return REGULAR
    return MIXED


def _observed_in(et_start: pd.Timestamp, minute_bars: Optional[pd.DataFrame], span: pd.Timedelta) -> Optional[int]:
    if minute_bars is None:
        return None
    if len(minute_bars) == 0:
        return 0
    if _naive_index(minute_bars):
        return None
    index = minute_bars.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    minutes = index.tz_convert(NEW_YORK).floor("min").unique()
    window_end = et_start + span
    inside = (minutes >= et_start) & (minutes < window_end)
    return int(inside.sum())

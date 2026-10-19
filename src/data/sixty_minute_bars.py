"""Reconstruct completed 60-minute session bars from SIP 30-minute bars.

This module does not fetch data and does not change a scan, a score, or an
order. Timestamps are the left edge of each bar. Aware timestamps are
normalized to UTC. Session membership is the America/New_York wall clock.

A 30-minute bar is usable only after its window has ended and that end is at
least 15 minutes before ``as_of``. The bar that starts at the session close
is never part of a regular 60-minute bar. The closing print Alpaca places on
that boundary is a separate 1-minute bar, exposed as ``closing_minute``. It
is the minute that starts at the close, not an official auction label.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional, Sequence

import pandas as pd

NEW_YORK = "America/New_York"
SIP_RECENT_DELAY = pd.Timedelta(minutes=15)
THIRTY_MINUTES = pd.Timedelta(minutes=30)
ONE_MINUTE = pd.Timedelta(minutes=1)
REGULAR_OPEN = time(9, 30)
BAR_COLUMNS = ("open", "high", "low", "close", "volume", "trade_count")

MISSING_HALF = "missing_half"
DUPLICATE_TIMESTAMP = "duplicate_timestamp"
INCOMPLETE_SIP_DELAY = "incomplete_sip_delay"
NAIVE_TIMESTAMP = "naive_timestamp"
STARTS_AT_SESSION_CLOSE = "starts_at_session_close"
OUTSIDE_REGULAR_SESSION = "outside_regular_session"
OFF_GRID = "off_grid"
INVALID_BAR = "invalid_bar"
CLOSING_MINUTE_MISSING = "closing_minute_missing"
CLOSING_MINUTE_INCOMPLETE = "closing_minute_incomplete"
CLOSING_MINUTE_DUPLICATE = "closing_minute_duplicate"


@dataclass(frozen=True)
class SixtyMinuteBar:
    """One completed regular-session hour. ``timestamp`` is its left edge, UTC."""

    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: float


@dataclass(frozen=True)
class ClosingMinute:
    """The 1-minute SIP bar that starts at the session close. Not an auction label."""

    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: float


@dataclass(frozen=True)
class BarRejection:
    """Why a window or an input bar did not become output."""

    reason: str
    timestamp: Optional[pd.Timestamp] = None
    window_start: Optional[pd.Timestamp] = None


@dataclass(frozen=True)
class ReconstructionResult:
    bars: tuple[SixtyMinuteBar, ...]
    closing_minute: Optional[ClosingMinute]
    rejections: tuple[BarRejection, ...]


def reconstruct_sixty_minute_bars(
    bars_30m: pd.DataFrame,
    *,
    session_day,
    session_close,
    as_of,
    closing_minutes: Optional[pd.DataFrame] = None,
) -> ReconstructionResult:
    """Pair completed 30-minute bars into regular 60-minute session windows.

    ``session_close`` is a New York wall time such as ``"16:00"`` or ``"13:00"``.
    Offset windows start at 09:30 and step by 60 minutes while they end at
    least 30 minutes before the close. The final window is the 60 minutes
    that end at the close, built from the two halves before that close.
    """
    day = _session_day(session_day)
    close_time = _close_time(session_close)
    as_of_utc, as_of_rejection = _as_utc(as_of)
    if as_of_rejection is not None:
        return ReconstructionResult((), None, (as_of_rejection,))

    halves, half_rejections, naive = _prepare(bars_30m)
    if naive:
        return ReconstructionResult((), None, (BarRejection(NAIVE_TIMESTAMP),))

    windows = _windows(day, close_time)
    bars: list[SixtyMinuteBar] = []
    rejections: list[BarRejection] = list(half_rejections)
    used: set[pd.Timestamp] = set()
    for window_start, first_edge, second_edge in windows:
        built, window_rejections = _pair(halves, window_start, first_edge, second_edge, as_of_utc)
        rejections.extend(window_rejections)
        if built is not None:
            bars.append(built)
            used.add(first_edge)
            used.add(second_edge)

    rejections.extend(_unused_halves(halves, used, day, close_time))
    closing, closing_rejections = _closing_minute(closing_minutes, day, close_time, as_of_utc)
    rejections.extend(closing_rejections)
    return ReconstructionResult(tuple(bars), closing, tuple(rejections))


def _windows(day: date, close_time: time) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    open_minute = _minutes(REGULAR_OPEN)
    close_minute = _minutes(close_time)
    windows = []
    start = open_minute
    while start + 60 <= close_minute - 30:
        windows.append(_window(day, start))
        start += 60
    final_start = close_minute - 60
    if final_start >= open_minute:
        final = _window(day, final_start)
        if final not in windows:
            windows.append(final)
    return windows


def _window(day: date, start_minute: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    start = _at(day, start_minute).tz_convert("UTC")
    first = start
    second = _at(day, start_minute + 30).tz_convert("UTC")
    return start, first, second


def _pair(halves, window_start, first_edge, second_edge, as_of_utc):
    rejections = []
    rows = []
    for edge in (first_edge, second_edge):
        row, rejection = _usable_half(halves, edge, window_start, as_of_utc)
        if rejection is not None:
            rejections.append(rejection)
        else:
            rows.append(row)
    if rejections:
        return None, rejections
    first, second = rows
    values = [_fields(row) for row in (first, second)]
    if any(item is None for item in values):
        return None, [BarRejection(INVALID_BAR, window_start=window_start)]
    left, right = values
    return (
        SixtyMinuteBar(
            timestamp=window_start,
            open=left[0],
            high=max(left[1], right[1]),
            low=min(left[2], right[2]),
            close=right[3],
            volume=_add(left[4], right[4]),
            trade_count=_add(left[5], right[5]),
        ),
        [],
    )


def _usable_half(halves, edge, window_start, as_of_utc):
    if edge in halves["duplicates"]:
        return None, BarRejection(DUPLICATE_TIMESTAMP, timestamp=edge, window_start=window_start)
    row = halves["rows"].get(edge)
    if row is None:
        return None, BarRejection(MISSING_HALF, timestamp=edge, window_start=window_start)
    if edge + THIRTY_MINUTES > as_of_utc - SIP_RECENT_DELAY:
        return None, BarRejection(INCOMPLETE_SIP_DELAY, timestamp=edge, window_start=window_start)
    return row, None


def _unused_halves(halves, used: set[pd.Timestamp], day: date, close_time: time) -> list[BarRejection]:
    close_edge = _at(day, _minutes(close_time)).tz_convert("UTC")
    open_edge = _at(day, _minutes(REGULAR_OPEN)).tz_convert("UTC")
    rejections = []
    for edge in sorted(halves["rows"]):
        if edge in used:
            continue
        et = edge.tz_convert(NEW_YORK)
        if et.date() != day:
            continue
        if edge == close_edge:
            rejections.append(BarRejection(STARTS_AT_SESSION_CLOSE, timestamp=edge))
            continue
        if edge < open_edge or edge >= close_edge:
            rejections.append(BarRejection(OUTSIDE_REGULAR_SESSION, timestamp=edge))
            continue
        if not _on_half_grid(et):
            rejections.append(BarRejection(OFF_GRID, timestamp=edge))
    return rejections


def _closing_minute(frame, day: date, close_time: time, as_of_utc):
    edge = _at(day, _minutes(close_time)).tz_convert("UTC")
    if frame is None:
        return None, [BarRejection(CLOSING_MINUTE_MISSING, timestamp=edge)]
    prepared, rejections, naive = _prepare(frame)
    if naive:
        return None, [BarRejection(NAIVE_TIMESTAMP, timestamp=edge)]
    if edge in prepared["duplicates"]:
        return None, [BarRejection(CLOSING_MINUTE_DUPLICATE, timestamp=edge)]
    row = prepared["rows"].get(edge)
    if row is None:
        return None, [BarRejection(CLOSING_MINUTE_MISSING, timestamp=edge)]
    if edge + ONE_MINUTE > as_of_utc - SIP_RECENT_DELAY:
        return None, [BarRejection(CLOSING_MINUTE_INCOMPLETE, timestamp=edge)]
    fields = _fields(row)
    if fields is None:
        return None, [BarRejection(INVALID_BAR, timestamp=edge)]
    minute = ClosingMinute(
        timestamp=edge,
        open=fields[0],
        high=fields[1],
        low=fields[2],
        close=fields[3],
        volume=fields[4],
        trade_count=fields[5],
    )
    return minute, rejections


def _prepare(frame: pd.DataFrame):
    empty = {"rows": {}, "duplicates": set()}
    if frame is None or len(frame) == 0:
        if frame is not None and any(column not in frame.columns for column in BAR_COLUMNS):
            return empty, [BarRejection(INVALID_BAR)], False
        return empty, [], False
    if any(column not in frame.columns for column in BAR_COLUMNS):
        return empty, [BarRejection(INVALID_BAR)], False
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index))
    if len(index) and index.tz is None:
        return empty, [], True
    rows = {}
    duplicates = set()
    rejections = []
    for pos in range(len(frame)):
        stamp = pd.Timestamp(index[pos])
        if stamp.tzinfo is None:
            return empty, [], True
        edge = stamp.tz_convert("UTC")
        if edge in duplicates:
            continue
        if edge in rows:
            duplicates.add(edge)
            del rows[edge]
            rejections.append(BarRejection(DUPLICATE_TIMESTAMP, timestamp=edge))
            continue
        rows[edge] = frame.iloc[pos]
    return {"rows": rows, "duplicates": duplicates}, rejections, False


def _fields(row) -> Optional[Sequence[float]]:
    parsed = []
    for column in BAR_COLUMNS:
        number = _finite(row[column])
        if number is None:
            return None
        parsed.append(number)
    return parsed


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _add(left: float, right: float):
    total = left + right
    if float(total).is_integer():
        return int(total)
    return total


def _as_utc(value) -> tuple[Optional[pd.Timestamp], Optional[BarRejection]]:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return None, BarRejection(NAIVE_TIMESTAMP)
    return stamp.tz_convert("UTC"), None


def _session_day(value) -> date:
    if isinstance(value, datetime):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(NEW_YORK)
        return stamp.date()
    if isinstance(value, date):
        return value
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(NEW_YORK)
    return stamp.date()


def _close_time(value) -> time:
    if isinstance(value, time):
        close = value
    else:
        text = str(value).strip()
        hour, minute = text.split(":")
        close = time(int(hour), int(minute))
    if close <= REGULAR_OPEN:
        raise ValueError(f"session_close must be after {REGULAR_OPEN.strftime('%H:%M')}")
    if close.second or close.microsecond:
        raise ValueError("session_close must be an exact minute")
    return close


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _at(day: date, minute_of_day: int) -> pd.Timestamp:
    hour, minute = divmod(minute_of_day, 60)
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, tz=NEW_YORK)


def _on_half_grid(et: pd.Timestamp) -> bool:
    return et.minute in (0, 30) and et.second == 0 and et.microsecond == 0 and et.nanosecond == 0

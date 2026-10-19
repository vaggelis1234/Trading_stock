"""Completed session windows for a scheduled Universe scan.

Sixty-minute windows are two completed ``30Min`` bars. The final window of
the session is the last half hour and stays 30 minutes. Bars that have not
finished, that are inside the Historical SIP delay, or that sit in
pre-market or after-hours are rejected. A missing half is not filled in.

This is the scan clock. It does not build the overlapping 15:00–16:00 hour
and it does not read a closing print.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

from src.data.sixty_minute_bars import (
    DUPLICATE_TIMESTAMP,
    INCOMPLETE_SIP_DELAY,
    INVALID_BAR,
    MISSING_HALF,
    NAIVE_TIMESTAMP,
    OFF_GRID,
    OUTSIDE_REGULAR_SESSION,
    SIP_RECENT_DELAY,
    THIRTY_MINUTES,
    BarRejection,
)
from src.universe.schedule import NEW_YORK, ScanSlot, as_new_york

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class ScheduledBar:
    """One completed scan window. ``timestamp`` is the left edge, UTC."""

    timestamp: pd.Timestamp
    window_end: pd.Timestamp
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[float]


@dataclass(frozen=True)
class ScheduledReconstruction:
    bars: tuple[ScheduledBar, ...]
    rejections: tuple[BarRejection, ...]

    def frame(self) -> pd.DataFrame:
        """OHLCV indexed at each window start. Empty when nothing completed."""
        if not self.bars:
            return pd.DataFrame(columns=list(BAR_COLUMNS))
        index = pd.DatetimeIndex([bar.timestamp for bar in self.bars])
        return pd.DataFrame(
            {
                "open": [bar.open for bar in self.bars],
                "high": [bar.high for bar in self.bars],
                "low": [bar.low for bar in self.bars],
                "close": [bar.close for bar in self.bars],
                "volume": [bar.volume for bar in self.bars],
            },
            index=index,
        )


def reconstruct_scheduled_bars(
    bars_30m: pd.DataFrame,
    slots: Sequence[ScanSlot],
    as_of,
) -> ScheduledReconstruction:
    """Build every slot window whose data is complete at ``as_of``."""
    as_of_utc, naive = _as_utc(as_of)
    if naive:
        return ScheduledReconstruction((), (BarRejection(NAIVE_TIMESTAMP),))
    prepared, rejections, naive_frame = _prepare(bars_30m)
    if naive_frame:
        return ScheduledReconstruction((), (BarRejection(NAIVE_TIMESTAMP),))
    built = []
    for slot in slots:
        window_end_utc = slot.window_end.tz_convert("UTC")
        if as_of_utc < window_end_utc:
            continue
        if as_of_utc < window_end_utc + SIP_RECENT_DELAY:
            rejections.append(BarRejection(INCOMPLETE_SIP_DELAY, window_start=slot.window_start.tz_convert("UTC")))
            continue
        bar, slot_rejections = _build_slot(prepared, slot, as_of_utc)
        rejections.extend(slot_rejections)
        if bar is not None:
            built.append(bar)
    return ScheduledReconstruction(tuple(built), tuple(rejections))


def _build_slot(prepared, slot: ScanSlot, as_of_utc: pd.Timestamp):
    rejections = []
    rows = []
    for edge in slot.halves:
        utc_edge = edge.tz_convert("UTC")
        row, rejection = _usable(prepared, utc_edge, slot, as_of_utc)
        if rejection is not None:
            rejections.append(rejection)
        else:
            rows.append(row)
    if rejections or len(rows) != len(slot.halves):
        return None, rejections
    values = [_fields(row) for row in rows]
    if any(item is None for item in values):
        return None, [BarRejection(INVALID_BAR, window_start=slot.window_start.tz_convert("UTC"))]
    first, last = values[0], values[-1]
    highs = [item[1] for item in values]
    lows = [item[2] for item in values]
    return (
        ScheduledBar(
            timestamp=slot.window_start.tz_convert("UTC"),
            window_end=slot.window_end.tz_convert("UTC"),
            minutes=slot.window_minutes,
            open=first[0],
            high=max(highs),
            low=min(lows),
            close=last[3],
            volume=_add(*(item[4] for item in values)),
            trade_count=_add_optional(*(item[5] for item in values)),
        ),
        [],
    )


def _usable(prepared, edge: pd.Timestamp, slot: ScanSlot, as_of_utc: pd.Timestamp):
    window_start = slot.window_start.tz_convert("UTC")
    et = edge.tz_convert(NEW_YORK)
    if et < slot.window_start or et >= slot.window_end:
        return None, BarRejection(OUTSIDE_REGULAR_SESSION, timestamp=edge, window_start=window_start)
    if not _on_half_grid(et):
        return None, BarRejection(OFF_GRID, timestamp=edge, window_start=window_start)
    if edge in prepared["duplicates"]:
        return None, BarRejection(DUPLICATE_TIMESTAMP, timestamp=edge, window_start=window_start)
    row = prepared["rows"].get(edge)
    if row is None:
        return None, BarRejection(MISSING_HALF, timestamp=edge, window_start=window_start)
    if edge + THIRTY_MINUTES > as_of_utc - SIP_RECENT_DELAY:
        return None, BarRejection(INCOMPLETE_SIP_DELAY, timestamp=edge, window_start=window_start)
    if edge < slot.window_start.tz_convert("UTC") or edge + THIRTY_MINUTES > slot.window_end.tz_convert("UTC"):
        return None, BarRejection(OUTSIDE_REGULAR_SESSION, timestamp=edge, window_start=window_start)
    return row, None


def _prepare(frame: pd.DataFrame):
    empty = {"rows": {}, "duplicates": set()}
    if frame is None or len(frame) == 0:
        return empty, [], False
    columns = [str(column).lower() for column in frame.columns]
    renamed = frame.copy()
    renamed.columns = columns
    if any(column not in renamed.columns for column in BAR_COLUMNS):
        return empty, [BarRejection(INVALID_BAR)], False
    index = renamed.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index))
    if len(index) and index.tz is None:
        return empty, [], True
    rows = {}
    duplicates = set()
    rejections = []
    for pos in range(len(renamed)):
        stamp = pd.Timestamp(index[pos])
        if stamp.tzinfo is None:
            return empty, [], True
        edge = stamp.tz_convert("UTC")
        et = edge.tz_convert(NEW_YORK)
        if not _on_half_grid(et):
            rejections.append(BarRejection(OFF_GRID, timestamp=edge))
            continue
        if edge in duplicates:
            continue
        if edge in rows:
            duplicates.add(edge)
            del rows[edge]
            rejections.append(BarRejection(DUPLICATE_TIMESTAMP, timestamp=edge))
            continue
        rows[edge] = renamed.iloc[pos]
    return {"rows": rows, "duplicates": duplicates}, rejections, False


def _fields(row) -> Optional[tuple]:
    parsed = []
    for column in BAR_COLUMNS:
        number = _finite(row[column])
        if number is None:
            return None
        parsed.append(number)
    trade_count = _finite(row["trade_count"]) if "trade_count" in row.index else None
    parsed.append(trade_count)
    return tuple(parsed)


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _add(*parts: float):
    total = sum(parts)
    if float(total).is_integer():
        return int(total)
    return total


def _add_optional(*parts: Optional[float]) -> Optional[float]:
    if any(part is None for part in parts):
        return None
    return _add(*parts)


def _as_utc(value):
    try:
        stamp = as_new_york(value)
    except ValueError:
        return None, True
    return stamp.tz_convert("UTC"), False


def _on_half_grid(et: pd.Timestamp) -> bool:
    return et.minute in (0, 30) and et.second == 0 and et.microsecond == 0 and et.nanosecond == 0

"""Universe scan clock for one cash session.

A regular session closes at 16:00 America/New_York. Each scan names the
completed window it is allowed to read. Windows are half-open and do not
overlap: the 15:45 scan reads 14:30–15:30, and the 16:15 scan reads only
15:30–16:00. Nothing here builds a 15:00–16:00 bar.

An early close keeps only the windows that finish at or before the real
close, then adds one final scan 15 minutes after that close. The final
window is the actual last half hour and never runs past 16:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Optional, Sequence

import pandas as pd

from src.market.calendar import REGULAR_CLOSE, REGULAR_OPEN, SessionHours

NEW_YORK = "America/New_York"
SLOT_GRACE = pd.Timedelta(minutes=5)
SIP_DELAY = pd.Timedelta(minutes=15)
HALF_HOUR = pd.Timedelta(minutes=30)
HOUR = pd.Timedelta(hours=1)
FINAL = "final"
REGULAR = "regular"


@dataclass(frozen=True)
class ScanSlot:
    """One scheduled scan. Timestamps are America/New_York."""

    session_date: date
    kind: str
    scan_at: pd.Timestamp
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    halves: tuple[pd.Timestamp, ...]

    @property
    def slot_id(self) -> str:
        return f"{self.session_date.isoformat()}T{self.scan_at.strftime('%H:%M')}"

    @property
    def opens_buys(self) -> bool:
        return self.kind != FINAL

    @property
    def window_minutes(self) -> int:
        return int((self.window_end - self.window_start).total_seconds() // 60)


def session_slots(session: Optional[SessionHours]) -> tuple[ScanSlot, ...]:
    """Scans for one session. Weekends and holidays return an empty plan."""
    if session is None:
        return ()
    close = _cap_close(session.close_et)
    open_at = _at(session.day, REGULAR_OPEN)
    close_at = _at(session.day, close)
    final_start = close_at - HALF_HOUR
    if final_start < open_at:
        return ()
    slots = []
    start = open_at
    while start + HOUR <= final_start:
        slots.append(_slot(session.day, REGULAR, start, start + HOUR))
        start += HOUR
    slots.append(_slot(session.day, FINAL, final_start, close_at))
    _require_disjoint(slots)
    return tuple(slots)


def classify_slots(
    now,
    slots: Sequence[ScanSlot],
    seen: set[str],
) -> tuple[tuple[ScanSlot, ...], tuple[ScanSlot, ...]]:
    """Split unseen slots into missed and currently due.

    A slot is due only inside its grace window. An older unseen slot is
    missed and must not be executed later in a batch. At most one slot is due.
    """
    moment = as_new_york(now)
    missed = []
    due = []
    for slot in slots:
        if slot.slot_id in seen:
            continue
        if moment < slot.scan_at:
            continue
        if moment >= slot.scan_at + SLOT_GRACE:
            missed.append(slot)
        else:
            due.append(slot)
    if len(due) > 1:
        missed.extend(due[:-1])
        due = due[-1:]
    return tuple(missed), tuple(due)


def as_new_york(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("scheduler clock must be timezone-aware")
    return stamp.tz_convert(NEW_YORK)


def _slot(day: date, kind: str, start: pd.Timestamp, end: pd.Timestamp) -> ScanSlot:
    halves = [start]
    if end - start > HALF_HOUR:
        halves.append(start + HALF_HOUR)
    return ScanSlot(
        session_date=day,
        kind=kind,
        scan_at=end + SIP_DELAY,
        window_start=start,
        window_end=end,
        halves=tuple(halves),
    )


def _cap_close(close: time) -> time:
    """A session cannot be extended past the regular 16:00 cash close."""
    if close > REGULAR_CLOSE:
        return REGULAR_CLOSE
    return close


def _at(day: date, wall: time) -> pd.Timestamp:
    return pd.Timestamp(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=wall.hour,
        minute=wall.minute,
        tz=NEW_YORK,
    )


def _require_disjoint(slots: Sequence[ScanSlot]) -> None:
    for earlier, later in zip(slots, slots[1:]):
        if earlier.window_end > later.window_start:
            raise ValueError(f"scan windows overlap: {earlier.slot_id} and {later.slot_id}")

"""NYSE cash-session calendar.

Sessions are America/New_York wall times. Weekends, full holidays, and
1:00 p.m. early closes come from the exchange rules. The clock conversion
uses the zone database, so EDT and EST are not a fixed UTC offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, time
from typing import Optional


REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True)
class SessionHours:
    """One cash session. Times are New York wall times."""

    day: date
    open_et: time
    close_et: time

    @property
    def early_close(self) -> bool:
        return self.close_et < REGULAR_CLOSE


class NyseCalendar:
    """Weekday sessions with NYSE holidays and scheduled early closes."""

    def session(self, day: date) -> Optional[SessionHours]:
        if day.weekday() >= 5 or day in self.holidays(day.year):
            return None
        close = EARLY_CLOSE if day in self.early_closes(day.year) else REGULAR_CLOSE
        return SessionHours(day, REGULAR_OPEN, close)

    def is_trading_day(self, day: date) -> bool:
        return self.session(day) is not None

    def recent_sessions(self, day: date, calendar_days: int) -> tuple[SessionHours, ...]:
        """Trading sessions in ``(day - calendar_days, day]``, oldest first."""
        found = []
        cursor = day
        start = day - timedelta(days=calendar_days)
        while cursor > start:
            session = self.session(cursor)
            if session is not None:
                found.append(session)
            cursor -= timedelta(days=1)
        found.reverse()
        return tuple(found)

    def holidays(self, year: int) -> frozenset[date]:
        days = set()
        for observed_day in (
            _observed(date(year, 1, 1)),
            _observed(date(year + 1, 1, 1)),
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            _good_friday(year),
            _last_weekday(year, 5, 0),
            _observed(date(year, 6, 19)) if year >= 2022 else None,
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        ):
            if observed_day is not None and observed_day.year == year:
                days.add(observed_day)
        return frozenset(days)

    def early_closes(self, year: int) -> frozenset[date]:
        """1:00 p.m. closes that are not already full holidays."""
        closed = self.holidays(year)
        candidates = {
            _nth_weekday(year, 11, 3, 4) + timedelta(days=1),
            date(year, 7, 3),
            date(year, 12, 24),
        }
        return frozenset(
            day for day in candidates if day.year == year and day.weekday() < 5 and day not in closed
        )


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _good_friday(year: int) -> date:
    return _easter(year) - timedelta(days=2)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm)."""
    century, year_mod = divmod(year, 100)
    century_mod4, century_mod = divmod(century, 4)
    leap_correction = (century + 8) // 25
    century_correction = (century - leap_correction + 1) // 3
    epact = (19 * (year % 19) + century - century_mod4 - century_correction + 15) % 30
    year_mod4, year_century_mod = divmod(year_mod, 4)
    weekday_shift = (32 + 2 * century_mod + 2 * year_mod4 - epact - year_century_mod) % 7
    month_correction = (year % 19 + 11 * epact + 22 * weekday_shift) // 451
    month, day_index = divmod(epact + weekday_shift - 7 * month_correction + 114, 31)
    return date(year, month, day_index + 1)

"""Download chosen cash sessions of 30-minute and daily bars.

A selected day is one NYSE session, from 09:30 ET to the official close.
Days that are not selected are not requested, including the sessions that
sit between two chosen dates. Weekends, holidays, and future sessions are
refused. This does not score the universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from src.data.bar_store import BarStore
from src.market.calendar import NyseCalendar

MAX_SESSIONS = 60
CALENDAR_LOOKBACK_DAYS = 60


class SessionChoiceError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NoTrackedSymbols(RuntimeError):
    """The bar store has no 30-minute symbols to refresh."""


@dataclass(frozen=True)
class DownloadResult:
    bars: int
    failed_symbols: tuple[str, ...]
    stopped: bool = False
    skipped: int = 0


def ny_today(clock=None) -> date:
    stamp = pd.Timestamp.now(tz="America/New_York") if clock is None else pd.Timestamp(clock)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York").date()


def parse_sessions(values: list[str], *, today: date) -> list[date]:
    """Unique cash sessions on or before ``today``, oldest first."""
    if not values:
        raise SessionChoiceError("empty")
    if len(values) > MAX_SESSIONS:
        raise SessionChoiceError("too_many")
    calendar = NyseCalendar()
    chosen: list[date] = []
    seen: set[date] = set()
    for value in values:
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            raise SessionChoiceError("not_a_session") from None
        if day in seen:
            continue
        seen.add(day)
        if calendar.session(day) is None or day > today:
            raise SessionChoiceError("future_session" if day > today else "not_a_session")
        chosen.append(day)
    chosen.sort()
    return chosen


def session_month(year: int, month: int, store: BarStore, today: date) -> dict:
    if month < 1 or month > 12 or year < 1990 or year > 2100:
        raise SessionChoiceError("bad_month")
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    symbols = tracked_symbols(store)
    counts = _bar_counts(store, first, nxt)
    absent = store.session_absences(first, nxt)
    now = _coverage_clock(today)
    calendar = NyseCalendar()
    days = []
    cursor = first
    while cursor < nxt:
        session = calendar.session(cursor)
        stored, complete, partial = _day_coverage(cursor, session, symbols, counts, absent, now)
        days.append(
            {
                "date": cursor.isoformat(),
                "market": session is not None,
                "close": None if session is None else session.close_et.strftime("%H:%M"),
                "stored_symbols": stored,
                "complete": complete,
                "partial": partial,
                "selectable": session is not None and cursor <= today,
            }
        )
        cursor += timedelta(days=1)
    return {
        "today": today.isoformat(),
        "year": year,
        "month": month,
        "symbols": _symbol_count(store),
        "days": days,
    }


def calendar_session_dates(today: date, calendar_days: int = CALENDAR_LOOKBACK_DAYS) -> list[date]:
    """Cash sessions inside the last ``calendar_days`` calendar days, including ``today``."""
    return [session.day for session in NyseCalendar().recent_sessions(today, calendar_days)]


def cash_bar_count(session) -> int:
    """30-minute bars from the open through the last bar that ends at the close."""
    open_minute = session.open_et.hour * 60 + session.open_et.minute
    close_minute = session.close_et.hour * 60 + session.close_et.minute
    return max(0, (close_minute - open_minute) // 30)


def symbols_still_missing(store: BarStore, dates: list[date], symbols: list[str], *, clock) -> dict[date, tuple[list[str], list[str]]]:
    """Symbols still missing 30-minute bars, and symbols still missing the daily bar.

    A closed session is complete only when every symbol has its full cash-session
    count and one daily bar. A symbol the provider confirmed has no bars is
    complete for that session. Anything less is downloaded for those symbols only.
    Before the close, only 30-minute bars that have already ended can be missing.
    """
    ordered = list(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not ordered or not dates:
        return {}
    now = pd.Timestamp(clock)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    now = now.tz_convert("America/New_York")
    calendar = NyseCalendar()
    start = min(dates)
    end = max(dates) + timedelta(days=1)
    counts = _bar_counts(store, start, end)
    absent = store.session_absences(start, end)
    missing: dict[date, tuple[list[str], list[str]]] = {}
    for day in dates:
        session = calendar.session(day)
        if session is None:
            continue
        expected, daily = _required_bars(session, now)
        bars = [
            symbol
            for symbol in ordered
            if counts.get((day, symbol, "30m"), 0) < expected and (day, symbol, "30m") not in absent
        ]
        daily_missing = [
            symbol
            for symbol in ordered
            if daily and counts.get((day, symbol, "1d"), 0) < 1 and (day, symbol, "1d") not in absent
        ]
        missing[day] = (bars, daily_missing)
    return missing


def download_calendar_days(
    dates: list[date],
    *,
    provider,
    store: BarStore,
    symbols: list[str],
    clock,
    on_progress=None,
    should_stop=None,
    wait_if_paused=None,
) -> DownloadResult:
    """Download each cash session and nothing else. This does not score the universe."""
    return download_sessions(
        dates,
        provider=provider,
        store=store,
        symbols=symbols,
        clock=clock,
        on_progress=on_progress,
        should_stop=should_stop,
        wait_if_paused=wait_if_paused,
    )


def download_scan_window(
    slot,
    *,
    provider,
    store: BarStore,
    symbols: list[str],
    clock,
    should_stop=None,
    wait_if_paused=None,
) -> DownloadResult:
    """Download the one completed window a scan slot is allowed to read.

    A regular slot stores that 30-minute bar. The final slot also stores the
    daily bar once the session has closed. Symbols that already have the bar
    are not requested again.
    """
    from src.universe.schedule import FINAL

    ordered = list(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not ordered:
        raise NoTrackedSymbols()
    if wait_if_paused is not None:
        wait_if_paused()
    if should_stop is not None and should_stop():
        return DownloadResult(0, (), stopped=True)
    needed = [symbol for symbol in ordered if symbol not in _symbols_holding(store, ordered, slot.window_start, "30m")]
    failed: list[str] = []
    bars = 0
    if needed:
        batch = provider.get_bars_many(
            needed,
            slot.window_start.isoformat(),
            slot.window_end.isoformat(),
            "30m",
        )
        for symbol, frame in batch.frames.items():
            bars += store.upsert_30m(symbol, frame, clock)
        for symbol in batch.failed:
            if symbol not in failed:
                failed.append(symbol)
    if should_stop is not None and should_stop():
        return DownloadResult(bars, tuple(failed), stopped=True)
    needed_daily: list[str] = []
    if slot.kind == FINAL:
        missing = symbols_still_missing(store, [slot.session_date], ordered, clock=clock)
        needed_daily = list(missing.get(slot.session_date, ([], []))[1])
        if needed_daily:
            day_start = pd.Timestamp(slot.session_date, tz="America/New_York")
            daily = provider.get_bars_many(
                needed_daily,
                day_start.isoformat(),
                (day_start + pd.Timedelta(days=1)).isoformat(),
                "1d",
            )
            for symbol, frame in daily.frames.items():
                bars += store.upsert_1d(symbol, frame, clock)
            for symbol in daily.failed:
                if symbol not in failed:
                    failed.append(symbol)
    skipped = 0 if needed or needed_daily else 1
    return DownloadResult(bars, tuple(failed), skipped=skipped)


def download_sessions(
    dates: list[date],
    *,
    provider,
    store: BarStore,
    symbols: list[str],
    clock,
    on_progress=None,
    should_stop=None,
    wait_if_paused=None,
) -> DownloadResult:
    """Fetch each chosen session and upsert its cash-session bars."""
    ordered = list(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not ordered:
        raise NoTrackedSymbols()
    calendar = NyseCalendar()
    failed: list[str] = []
    bars = 0
    skipped = 0
    total = len(dates)
    missing = symbols_still_missing(store, dates, ordered, clock=clock)
    for index, day in enumerate(dates):
        if wait_if_paused is not None:
            wait_if_paused()
        if should_stop is not None and should_stop():
            return DownloadResult(bars, tuple(failed), stopped=True, skipped=skipped)
        needed_bars, needed_daily = missing.get(day, (ordered, ordered))
        if not needed_bars and not needed_daily:
            skipped += 1
            if on_progress is not None:
                on_progress(index + 1, total)
            continue
        session = calendar.session(day)
        if session is None:
            raise SessionChoiceError("not_a_session")
        start = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(hours=9, minutes=30)
        end = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(
            hours=session.close_et.hour, minutes=session.close_et.minute
        )
        closed = _session_closed(session, clock)
        if needed_bars:
            batch = provider.get_bars_many(needed_bars, start.isoformat(), end.isoformat(), "30m")
            for symbol, frame in batch.frames.items():
                written = store.upsert_30m(symbol, frame, clock)
                bars += written
                if closed and written == 0 and _empty_frame(frame):
                    store.mark_session_absent(symbol, day, "30m")
            for symbol in batch.failed:
                if symbol not in failed:
                    failed.append(symbol)
        day_start = pd.Timestamp(day, tz="America/New_York")
        if needed_daily:
            daily = provider.get_bars_many(
                needed_daily,
                day_start.isoformat(),
                (day_start + pd.Timedelta(days=1)).isoformat(),
                "1d",
            )
            for symbol, frame in daily.frames.items():
                written = store.upsert_1d(symbol, frame, clock)
                bars += written
                if closed and written == 0 and _empty_frame(frame):
                    store.mark_session_absent(symbol, day, "1d")
            for symbol in daily.failed:
                if symbol not in failed:
                    failed.append(symbol)
        if on_progress is not None:
            on_progress(index + 1, total)
    return DownloadResult(bars, tuple(failed), skipped=skipped)


def delete_sessions(dates: list[date], *, store: BarStore) -> int:
    """Remove stored 30-minute and daily bars for the chosen New York dates."""
    chosen = list(dates)
    return store.delete_30m_days(chosen) + store.delete_1d_days(chosen)


def tracked_symbols(store: BarStore) -> list[str]:
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM market_bars WHERE interval = '30m' ORDER BY symbol"
        ).fetchall()
    finally:
        conn.close()
    return [row["symbol"] for row in rows]


def _symbols_holding(store: BarStore, symbols: list[str], timestamp, interval: str) -> set[str]:
    """Symbols that already store ``interval`` at ``timestamp``."""
    from src.data.bar_store import _utc_iso

    wanted = set(symbols)
    if not wanted:
        return set()
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT symbol FROM market_bars WHERE interval = ? AND bar_timestamp = ?",
            (interval, _utc_iso(timestamp)),
        ).fetchall()
    finally:
        conn.close()
    return {row["symbol"] for row in rows if row["symbol"] in wanted}


def _symbol_count(store: BarStore) -> int:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM market_bars WHERE interval = '30m'"
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


def _coverage_clock(today: date) -> pd.Timestamp:
    """Clock used by the month view. A past ``today`` is already closed."""
    now = pd.Timestamp.now(tz="America/New_York")
    if today < now.date():
        return pd.Timestamp(today, tz="America/New_York") + pd.Timedelta(hours=23)
    return now


def _required_bars(session, now: pd.Timestamp) -> tuple[int, bool]:
    """30-minute bars that should exist by ``now``, and whether the daily bar is due."""
    full = cash_bar_count(session)
    close = pd.Timestamp(session.day, tz="America/New_York") + pd.Timedelta(
        hours=session.close_et.hour, minutes=session.close_et.minute
    )
    if now >= close:
        return full, True
    if now.date() != session.day:
        return full, True
    minute = now.hour * 60 + now.minute
    open_minute = session.open_et.hour * 60 + session.open_et.minute
    closed = max(0, (minute - open_minute) // 30)
    return min(full, closed), False


def _session_closed(session, clock) -> bool:
    now = pd.Timestamp(clock) if clock is not None else pd.Timestamp.now(tz="UTC")
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    now = now.tz_convert("America/New_York")
    _, daily = _required_bars(session, now)
    return daily


def _empty_frame(frame) -> bool:
    return frame is None or bool(getattr(frame, "empty", True))


def _day_coverage(day: date, session, symbols: list[str], counts: dict, absent: set, now: pd.Timestamp) -> tuple[int, bool, bool]:
    """Stored symbol count, whether the day is complete, and whether it is only partial."""
    if session is None or not symbols:
        return 0, False, False
    expected, daily = _required_bars(session, now)
    stored = 0
    ready = 0
    for symbol in symbols:
        bars = counts.get((day, symbol, "30m"), 0)
        if bars:
            stored += 1
        has_daily = counts.get((day, symbol, "1d"), 0) >= 1
        bars_ready = bars >= expected or (day, symbol, "30m") in absent
        daily_ready = has_daily or not daily or (day, symbol, "1d") in absent
        if bars_ready and daily_ready:
            ready += 1
    complete = bool(daily) and ready == len(symbols)
    partial = (stored > 0 or any(counts.get((day, symbol, "1d"), 0) for symbol in symbols)) and not complete
    return stored, complete, partial


def _bar_counts(store: BarStore, start: date, end: date) -> dict[tuple[date, str, str], int]:
    """Bar counts keyed by New York date, symbol, and interval."""
    start_iso = pd.Timestamp(start, tz="America/New_York").tz_convert("UTC").isoformat()
    end_iso = pd.Timestamp(end, tz="America/New_York").tz_convert("UTC").isoformat()
    conn = store._connect()
    try:
        rows = conn.execute(
            """SELECT interval, symbol, substr(bar_timestamp, 1, 10) AS day, COUNT(*) AS n
               FROM market_bars
               WHERE interval IN ('30m', '1d')
                 AND bar_timestamp >= ? AND bar_timestamp < ?
               GROUP BY day, symbol, interval""",
            (start_iso, end_iso),
        ).fetchall()
    finally:
        conn.close()
    counts: dict[tuple[date, str, str], int] = {}
    for row in rows:
        counts[(date.fromisoformat(row["day"]), row["symbol"], row["interval"])] = int(row["n"])
    return counts

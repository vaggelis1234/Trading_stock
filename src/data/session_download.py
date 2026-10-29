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
    counts = _stored_counts(store, first, nxt)
    calendar = NyseCalendar()
    days = []
    cursor = first
    while cursor < nxt:
        session = calendar.session(cursor)
        days.append(
            {
                "date": cursor.isoformat(),
                "market": session is not None,
                "close": None if session is None else session.close_et.strftime("%H:%M"),
                "stored_symbols": counts.get(cursor, 0),
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


def sessions_already_stored(store: BarStore, dates: list[date], symbols: list[str], *, clock) -> set[date]:
    """Sessions whose requested symbols already have a full cash day and a daily bar.

    The session that is still open is never treated as stored. One local query
    covers the whole span, so a full database does not ask the market again.
    """
    ordered = list(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not ordered or not dates:
        return set()
    now = pd.Timestamp(clock)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    now = now.tz_convert("America/New_York")
    calendar = NyseCalendar()
    needed: dict[date, int] = {}
    for day in dates:
        session = calendar.session(day)
        if session is None:
            continue
        close = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(
            hours=session.close_et.hour, minutes=session.close_et.minute
        )
        if now < close:
            continue
        needed[day] = cash_bar_count(session)
    if not needed:
        return set()
    counts = _bar_counts(store, min(needed), max(needed) + timedelta(days=1))
    ready: set[date] = set()
    for day, expected in needed.items():
        if all(
            counts.get((day, symbol, "30m"), 0) >= expected and counts.get((day, symbol, "1d"), 0) >= 1
            for symbol in ordered
        ):
            ready.add(day)
    return ready


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
    ready = sessions_already_stored(store, dates, ordered, clock=clock)
    for index, day in enumerate(dates):
        if wait_if_paused is not None:
            wait_if_paused()
        if should_stop is not None and should_stop():
            return DownloadResult(bars, tuple(failed), stopped=True, skipped=skipped)
        if day in ready:
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
        batch = provider.get_bars_many(ordered, start.isoformat(), end.isoformat(), "30m")
        for symbol, frame in batch.frames.items():
            bars += store.upsert_30m(symbol, frame, clock)
        for symbol in batch.failed:
            if symbol not in failed:
                failed.append(symbol)
        day_start = pd.Timestamp(day, tz="America/New_York")
        daily = provider.get_bars_many(
            ordered,
            day_start.isoformat(),
            (day_start + pd.Timedelta(days=1)).isoformat(),
            "1d",
        )
        for symbol, frame in daily.frames.items():
            bars += store.upsert_1d(symbol, frame, clock)
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


def _symbol_count(store: BarStore) -> int:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM market_bars WHERE interval = '30m'"
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"])


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


def _stored_counts(store: BarStore, start: date, end: date) -> dict[date, int]:
    start_iso = pd.Timestamp(start, tz="America/New_York").tz_convert("UTC").isoformat()
    end_iso = pd.Timestamp(end, tz="America/New_York").tz_convert("UTC").isoformat()
    conn = store._connect()
    try:
        rows = conn.execute(
            """SELECT symbol, bar_timestamp FROM market_bars
               WHERE interval = '30m' AND bar_timestamp >= ? AND bar_timestamp < ?""",
            (start_iso, end_iso),
        ).fetchall()
    finally:
        conn.close()
    symbols: dict[date, set[str]] = {}
    for row in rows:
        day = pd.Timestamp(row["bar_timestamp"]).tz_convert("America/New_York").date()
        symbols.setdefault(day, set()).add(row["symbol"])
    return {day: len(names) for day, names in symbols.items()}

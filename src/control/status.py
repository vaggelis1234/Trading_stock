"""Read-only Trading Bot status.

Nothing in this module starts a service, opens a broker client, or writes a
database. Missing facts stay ``Unknown`` or ``Unavailable``.
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.control.permissions import NO_BUY, trading_state
from src.market.calendar import NyseCalendar
from src.universe.schedule import SLOT_GRACE, session_slots

UNKNOWN = "Unknown"
UNAVAILABLE = "Unavailable"
ATHENS = "Europe/Athens"
NEW_YORK = "America/New_York"


@dataclass(frozen=True)
class StatusReport:
    universe_running: str
    scheduler_active: str
    watch_engine_active: str
    last_scan_at: str
    last_scan_result: str
    next_slot_new_york: str
    next_slot_athens: str
    feed_health: str
    buy_permission: str
    trading_execution: str
    buys_active: str
    recent_errors: tuple[str, ...]

    def lines(self) -> list[str]:
        errors = "None" if not self.recent_errors else "\n".join(self.recent_errors)
        return [
            f"Universe running: {self.universe_running}",
            f"Scheduler active: {self.scheduler_active}",
            f"Watch Engine active: {self.watch_engine_active}",
            f"Last Universe scan: {self.last_scan_at}",
            f"Last Universe scan result: {self.last_scan_result}",
            f"Next slot (America/New_York): {self.next_slot_new_york}",
            f"Next slot (Europe/Athens): {self.next_slot_athens}",
            f"Market data feed: {self.feed_health}",
            f"Buy permission: {self.buy_permission}",
            f"Trading execution: {self.trading_execution}",
            f"Buys active: {self.buys_active}",
            f"Recent errors: {errors}",
        ]


def current_status() -> StatusReport:
    """Status of the default artifact paths. Does not create those files."""
    artifact_dir = os.getenv("ARTIFACT_DIR", "artifacts")
    return read_status(
        status_path=os.getenv("CONTROL_STATUS", os.path.join(artifact_dir, "control_status.json")),
        universe_db=os.getenv("UNIVERSE_DB", os.path.join(artifact_dir, "universe.db")),
        runtime_db=os.getenv("RUNTIME_DB", os.path.join(artifact_dir, "runtime.db")),
    )


def read_status(
    *,
    status_path: str,
    universe_db: str,
    runtime_db: str,
    now=None,
    calendar: NyseCalendar | None = None,
) -> StatusReport:
    """Describe the bot from files and the session calendar. Does not start it."""
    moment = pd.Timestamp.now(tz=NEW_YORK) if now is None else pd.Timestamp(now).tz_convert(NEW_YORK)
    published = _read_json(status_path)
    live = bool(published) and _pid_alive(published.get("pid"))
    state = trading_state(published.get("buy_permission", NO_BUY) if live else NO_BUY)
    last_at, last_result = _last_scan(published if live or published else None, universe_db)
    next_ny, next_athens = _next_slot(moment, calendar or NyseCalendar(), runtime_db)
    if live:
        universe = _yes_no(published.get("universe_running"))
        scheduler = _yes_no(published.get("scheduler_active"))
        watch = _yes_no(published.get("watch_engine_active"))
        feed = str(published.get("feed_health") or UNAVAILABLE)
        errors = tuple(str(item) for item in published.get("recent_errors") or [])
        permission = state.buy_permission
    else:
        universe = "no"
        scheduler = "no"
        watch = "no"
        feed = UNAVAILABLE
        errors = tuple(str(item) for item in (published or {}).get("recent_errors") or [])
        if published is None:
            errors = (UNKNOWN,)
        permission = NO_BUY
    return StatusReport(
        universe_running=universe,
        scheduler_active=scheduler,
        watch_engine_active=watch,
        last_scan_at=last_at,
        last_scan_result=last_result,
        next_slot_new_york=next_ny,
        next_slot_athens=next_athens,
        feed_health=feed,
        buy_permission=permission,
        trading_execution=state.trading_execution,
        buys_active="yes" if state.buys_active else "no",
        recent_errors=errors,
    )


def _yes_no(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return UNKNOWN


def _read_json(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _last_scan(published, universe_db: str) -> tuple[str, str]:
    if published and published.get("last_scan_at"):
        return str(published.get("last_scan_at")), str(published.get("last_scan_result") or UNKNOWN)
    stamp = _latest_snapshot_timestamp(universe_db)
    if stamp is None:
        return UNKNOWN, UNKNOWN
    return stamp, "persisted"


def _latest_snapshot_timestamp(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"{_uri(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT MAX(scan_timestamp) FROM universe_snapshots").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _next_slot(now: pd.Timestamp, calendar: NyseCalendar, runtime_db: str) -> tuple[str, str]:
    for offset in range(0, 12):
        day = now.date() + timedelta(days=offset)
        session = calendar.session(day)
        if session is None:
            continue
        known = _known_slots(runtime_db, day)
        if known is None:
            return UNKNOWN, UNKNOWN
        for slot in session_slots(session):
            if slot.slot_id in known or now >= slot.scan_at + SLOT_GRACE:
                continue
            ny = slot.scan_at.strftime("%Y-%m-%d %H:%M %Z")
            athens = slot.scan_at.tz_convert(ATHENS).strftime("%Y-%m-%d %H:%M %Z")
            return ny, athens
    return UNAVAILABLE, UNAVAILABLE


def _known_slots(path: str, day: date):
    if not path or not os.path.exists(path):
        return set()
    try:
        conn = sqlite3.connect(f"{_uri(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT slot_id FROM universe_schedule_slots WHERE session_date = ?",
            (day.isoformat(),),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _pid_alive(pid) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    handle = kernel32_open_process(value)
    return handle


def kernel32_open_process(pid: int) -> bool:
    process_query = 0x1000
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(process_query, False, pid)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True

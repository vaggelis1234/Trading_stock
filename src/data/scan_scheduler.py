"""Clock for automatic cash-session downloads.

A scan reads one completed 30-minute window, 15 minutes after that window
ends. Starting the clock announces the next scan. When that time arrives it
downloads the window, then announces the scan after it. Missed scans are not
replayed once their grace window has passed.
"""

from __future__ import annotations

import time
from datetime import timedelta

from src.market.calendar import NyseCalendar
from src.universe.schedule import ScanSlot, as_new_york, classify_slots, session_slots

POLL_SECONDS = 1.0
SCAN_FINISHED = "σκαν τελείωσε"
ATHENS = "Europe/Athens"


def next_scan_message(when: str, period: str) -> str:
    return f"επόμενο scan {when} για την περίοδο [{period}]"


def scan_begins_message(period: str) -> str:
    return f"σκαν ξεκινάει για την περίοδο [{period}]"


def _clock(stamp) -> str:
    """``HH:MM:SS`` on the timestamp's own wall clock."""
    return f"{int(stamp.hour):02d}:{int(stamp.minute):02d}:{int(stamp.second):02d}"


def scan_period(slot: ScanSlot) -> str:
    """New York cash window, ``HH:MM:SS-HH:MM:SS``."""
    return f"{_clock(slot.window_start)}-{_clock(slot.window_end)}"


def format_scan_clock(slot: ScanSlot, now) -> str:
    """Greek scan time as ``HH:MM:SS``. A later Athens date keeps its calendar day."""
    scan = as_new_york(slot.scan_at).tz_convert(ATHENS)
    moment = as_new_york(now).tz_convert(ATHENS)
    clock = _clock(scan)
    if scan.date() != moment.date():
        return f"{scan.date().isoformat()} {clock}"
    return clock


def due_scan(now, calendar: NyseCalendar | None = None) -> ScanSlot | None:
    """The slot inside its grace window, if the clock has reached it."""
    moment = as_new_york(now)
    calendar = calendar or NyseCalendar()
    session = calendar.session(moment.date())
    if session is None:
        return None
    _missed, due = classify_slots(moment, session_slots(session), set())
    if not due:
        return None
    return due[0]


def upcoming_scan(now, calendar: NyseCalendar | None = None) -> ScanSlot:
    """The next slot whose scan time is still ahead of ``now``."""
    moment = as_new_york(now)
    calendar = calendar or NyseCalendar()
    day = moment.date()
    for _ in range(21):
        session = calendar.session(day)
        if session is not None:
            for slot in session_slots(session):
                if slot.scan_at > moment:
                    return slot
        day += timedelta(days=1)
    raise LookupError("no upcoming cash session")


def run_download_scheduler(
    *,
    clock,
    download,
    log,
    should_stop,
    wait_if_paused=None,
    sleep=None,
    calendar: NyseCalendar | None = None,
) -> None:
    """Wait for each scan time, download that window, then name the next one."""
    sleep = time.sleep if sleep is None else sleep
    calendar = calendar or NyseCalendar()
    announced = None
    completed: set[str] = set()
    while not should_stop():
        if wait_if_paused is not None:
            wait_if_paused()
        if should_stop():
            return
        now = clock()
        slot = due_scan(now, calendar)
        if slot is not None and slot.slot_id not in completed:
            log(scan_begins_message(scan_period(slot)))
            if should_stop():
                return
            try:
                result = download(slot)
            except Exception as exc:
                log(f"σκαν δεν ολοκληρώθηκε: {exc}")
                completed.add(slot.slot_id)
                result = None
            else:
                if result is not None and getattr(result, "stopped", False):
                    return
                if should_stop():
                    return
                log(SCAN_FINISHED)
                if getattr(result, "failed_symbols", ()):
                    log(f"απέτυχαν {len(result.failed_symbols)} σύμβολα")
                completed.add(slot.slot_id)
            if should_stop():
                return
            nxt = upcoming_scan(clock(), calendar)
            log(next_scan_message(format_scan_clock(nxt, clock()), scan_period(nxt)))
            announced = nxt.slot_id
            continue
        nxt = upcoming_scan(now, calendar)
        if announced != nxt.slot_id:
            log(next_scan_message(format_scan_clock(nxt, clock()), scan_period(nxt)))
            announced = nxt.slot_id
        delay = (nxt.scan_at - as_new_york(clock())).total_seconds()
        step = POLL_SECONDS if delay <= 0 else min(POLL_SECONDS, delay)
        sleep(step)

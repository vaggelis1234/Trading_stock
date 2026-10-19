"""Session loop: Universe slots, then the watch engine.

Startup does not fire a scan by itself. A tick runs a slot only when that
slot is inside its grace window and has not already been claimed. Older
unseen slots are recorded as missed.
"""

from __future__ import annotations

import logging
import threading

import pandas as pd

from src.market.calendar import NyseCalendar
from src.persistence.store import load_latest_scan
from src.universe.schedule import ScanSlot, as_new_york, classify_slots, session_slots
from src.universe.schedule_store import ScheduleStore
from src.watch.engine import WatchEngine

logger = logging.getLogger(__name__)


class SessionRunner:
    def __init__(
        self,
        *,
        calendar: NyseCalendar,
        schedule_store: ScheduleStore,
        watch: WatchEngine,
        scan_fn,
        universe_db: str = "artifacts/universe.db",
    ):
        self.calendar = calendar
        self.store = schedule_store
        self.watch = watch
        self.scan_fn = scan_fn
        self.universe_db = universe_db
        self._lock = threading.Lock()
        self._resumed = False
        self.scans: list[str] = []

    def tick(self, now) -> None:
        with self._lock:
            self._tick(as_new_york(now))

    def _tick(self, now: pd.Timestamp) -> None:
        session = self.calendar.session(now.date())
        if session is None:
            logger.info("no cash session on %s; no universe scan was run", now.date().isoformat())
            return
        self._resume(now, session)
        slots = session_slots(session)
        missed, due = classify_slots(now, slots, self.store.known(session.day))
        for slot in missed:
            self.store.record_missed(slot, "grace window elapsed; slot was not replayed")
            logger.warning("missed universe slot %s; it will not be replayed", slot.slot_id)
        for slot in due:
            self._execute(slot, now)
        self.watch.tick(now, session.close_et)

    def _execute(self, slot: ScanSlot, now: pd.Timestamp) -> None:
        if not self.store.claim(slot):
            return
        try:
            result = self.scan_fn(slot, now)
        except Exception:
            logger.exception("universe slot %s failed", slot.slot_id)
            result = None
        ok = bool(result is not None and getattr(result, "ok", False))
        if ok:
            self.store.finish(slot, "executed")
            self.scans.append(slot.slot_id)
            self.watch.on_scan_success(slot, list(getattr(result, "rows", []) or []))
            return
        self.store.finish(slot, "failed", "scan failed")
        self.watch.on_scan_failed(slot)
        logger.warning("universe slot %s failed once and will not be replayed", slot.slot_id)

    def _resume(self, now: pd.Timestamp, session) -> None:
        if self._resumed:
            return
        self._resumed = True
        stuck = self.store.fail_stuck(session.day)
        if now.time() >= session.close_et:
            for slot_id in stuck:
                self.watch.on_scan_failed(slot_id)
            return
        if not self.store.has_successful_regular(session.day):
            for slot_id in stuck:
                self.watch.on_scan_failed(slot_id)
            return
        rows = load_latest_scan(self.universe_db)
        if rows and _on_session(rows[0].get("scan_timestamp"), session.day):
            self.watch.resume(rows)
        for slot_id in stuck:
            self.watch.on_scan_failed(slot_id)


def _on_session(value, day) -> bool:
    if value is None:
        return False
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York").date() == day

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
from src.persistence.store import SNAPSHOT_LOOKBACK, load_latest_scan
from src.universe.schedule import SLOT_GRACE, ScanSlot, as_new_york, classify_slots, session_slots
from src.universe.schedule_store import ScheduleStore
from src.watch.engine import ABSOLUTE_BUY_CUTOFF, WatchEngine

logger = logging.getLogger(__name__)


class SessionRunner:
    def __init__(
        self,
        *,
        calendar: NyseCalendar,
        schedule_store: ScheduleStore,
        watch: WatchEngine | None,
        scan_fn,
        universe_db: str = "artifacts/universe.db",
        watch_enabled: bool = True,
    ):
        if watch_enabled and watch is None:
            raise ValueError("watch engine is required when watch_enabled is true")
        self.calendar = calendar
        self.store = schedule_store
        self.watch = watch
        self.watch_enabled = watch_enabled
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
        self._tick_watch(now, session.close_et)

    def tick_watch(self, now) -> None:
        """One watch minute. Does not claim or run a Universe slot."""
        if not self.watch_enabled or self.watch is None:
            return
        with self._lock:
            moment = as_new_york(now)
            session = self.calendar.session(moment.date())
            if session is None:
                logger.info("no cash session on %s; watch was not run", moment.date().isoformat())
                return
            self._resume(moment, session)
            self.watch.tick(moment, session.close_et)

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
            if self.watch_enabled and self.watch is not None:
                self.watch.on_scan_success(slot, list(getattr(result, "rows", []) or []))
            return
        self.store.finish(slot, "failed", "scan failed")
        if self.watch_enabled and self.watch is not None:
            self.watch.on_scan_failed(slot)
        logger.warning("universe slot %s failed once and will not be replayed", slot.slot_id)

    def _resume(self, now: pd.Timestamp, session) -> None:
        if self._resumed:
            return
        self._resumed = True
        stuck = self.store.fail_stuck(session.day)
        if not self.watch_enabled or self.watch is None:
            return
        if now.time() >= session.close_et:
            for slot_id in stuck:
                self.watch.on_scan_failed(slot_id)
            return
        rows = _decision_rows(load_latest_scan(self.universe_db))
        on_session = bool(rows) and _on_session(rows[0].get("scan_timestamp"), session.day)
        scheduled = self.store.has_successful_regular(session.day)
        if on_session and (scheduled or _in_buy_window(rows[0].get("scan_timestamp"), session)):
            self.watch.resume(rows)
        for slot_id in stuck:
            self.watch.on_scan_failed(slot_id)

    def _tick_watch(self, now: pd.Timestamp, session_close) -> None:
        if self.watch_enabled and self.watch is not None:
            self.watch.tick(now, session_close)


def manual_scan_action(now, session, known: set[str]) -> str:
    """How a manual Universe start should meet the session clock.

    ``due_slot`` means a calendar slot is still inside its grace window and
    must be claimed by the session runner. ``already_recorded`` means that
    same window was already stored, so a second scan would duplicate it.
    ``standalone`` is a manual snapshot between slots.
    """
    if session is None:
        return "standalone"
    moment = as_new_york(now)
    slots = session_slots(session)
    _missed, due = classify_slots(moment, slots, known)
    if due:
        return "due_slot"
    for slot in slots:
        if slot.slot_id in known and slot.scan_at <= moment < slot.scan_at + SLOT_GRACE:
            return "already_recorded"
    return "standalone"


def _decision_rows(rows: list) -> list:
    if rows and rows[0].get("snapshot_kind") == SNAPSHOT_LOOKBACK:
        return []
    return rows


def _in_buy_window(value, session) -> bool:
    stamp = _as_new_york(value)
    if stamp is None:
        return False
    wall = stamp.time()
    return session.open_et <= wall < session.close_et and wall < ABSOLUTE_BUY_CUTOFF


def _as_new_york(value):
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York")


def _on_session(value, day) -> bool:
    if value is None:
        return False
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York").date() == day

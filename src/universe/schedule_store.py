"""One row per scheduled Universe slot.

A claimed slot is not run a second time. A crash leaves the row ``running``;
the next tick marks that row failed and does not replay it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Optional

from src.universe.schedule import REGULAR, ScanSlot


class ScheduleStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def known(self, session_date: date) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slot_id FROM universe_schedule_slots WHERE session_date = ?",
                (session_date.isoformat(),),
            ).fetchall()
        return {row[0] for row in rows}

    def claim(self, slot: ScanSlot) -> bool:
        """Reserve ``slot``. False when this session already recorded it."""
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO universe_schedule_slots
                       (slot_id, session_date, kind, status, detail)
                       VALUES (?, ?, ?, 'running', '')""",
                    (slot.slot_id, slot.session_date.isoformat(), slot.kind),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def finish(self, slot: ScanSlot, status: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE universe_schedule_slots
                   SET status = ?, detail = ?
                   WHERE slot_id = ?""",
                (status, detail, slot.slot_id),
            )

    def record_missed(self, slot: ScanSlot, detail: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO universe_schedule_slots
                   (slot_id, session_date, kind, status, detail)
                   VALUES (?, ?, ?, 'missed', ?)""",
                (slot.slot_id, slot.session_date.isoformat(), slot.kind, detail),
            )

    def status(self, slot_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM universe_schedule_slots WHERE slot_id = ?",
                (slot_id,),
            ).fetchone()
        return None if row is None else row[0]

    def has_successful_regular(self, session_date: date) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM universe_schedule_slots
                   WHERE session_date = ? AND kind = ? AND status = 'executed'""",
                (session_date.isoformat(), REGULAR),
            ).fetchone()
        return row is not None

    def fail_stuck(self, session_date: date) -> tuple[str, ...]:
        """Mark crashed in-progress slots failed. They are not replayed."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT slot_id FROM universe_schedule_slots
                   WHERE session_date = ? AND status = 'running'""",
                (session_date.isoformat(),),
            ).fetchall()
            ids = tuple(row[0] for row in rows)
            if ids:
                conn.executemany(
                    """UPDATE universe_schedule_slots
                       SET status = 'failed', detail = 'interrupted'
                       WHERE slot_id = ?""",
                    [(slot_id,) for slot_id in ids],
                )
        return ids

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS universe_schedule_slots (
                    slot_id TEXT PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                )"""
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


__all__ = ["ScheduleStore"]

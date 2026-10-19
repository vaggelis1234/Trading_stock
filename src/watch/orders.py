"""Persisted order attempts for the watch list.

A symbol with a submitted, uncertain, working, partial, or filled buy is not
sent again. A restart reads the same rows. Rejected and canceled buys block
another buy for that session only.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Optional


OPEN_STATUSES = frozenset({"submitted", "working", "partial", "filled", "submit_uncertain", "reserved"})
SESSION_BLOCK = frozenset({"rejected", "canceled", "expired"})


class OrderStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def reserve(self, symbol: str, session_date: date, qty: float, price: float) -> bool:
        if symbol in self.blocked_symbols(session_date):
            return False
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO watch_orders
                   (symbol, session_date, side, qty, status, broker_id, filled_qty, limit_price)
                   VALUES (?, ?, 'buy', ?, 'reserved', '', 0, ?)""",
                (symbol, session_date.isoformat(), qty, price),
            )
        return True

    def mark(self, symbol: str, session_date: date, status: str, broker_id: str = "", filled_qty: float = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE watch_orders
                   SET status = ?, broker_id = ?, filled_qty = ?
                   WHERE id = (
                       SELECT id FROM watch_orders
                       WHERE symbol = ? AND session_date = ? AND side = 'buy'
                       ORDER BY id DESC LIMIT 1
                   )""",
                (status, broker_id, filled_qty, symbol, session_date.isoformat()),
            )

    def release_reservation(self, symbol: str, session_date: date) -> None:
        """Drop a reservation that never reached the broker."""
        with self._connect() as conn:
            conn.execute(
                """DELETE FROM watch_orders
                   WHERE symbol = ? AND session_date = ? AND status = 'reserved'""",
                (symbol, session_date.isoformat()),
            )

    def blocked_symbols(self, session_date: date) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, session_date, status FROM watch_orders WHERE side = 'buy'"
            ).fetchall()
        blocked = set()
        for symbol, stored_day, status in rows:
            if status in OPEN_STATUSES or (status in SESSION_BLOCK and stored_day == session_date.isoformat()):
                blocked.add(symbol)
        return blocked

    def position_symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT symbol FROM watch_orders
                   WHERE side = 'buy' AND status IN ('submitted', 'working', 'partial', 'filled')
                   ORDER BY symbol"""
            ).fetchall()
        return [row[0] for row in rows]

    def open_orders(self) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, symbol, session_date, status, broker_id, filled_qty, qty
                   FROM watch_orders
                   WHERE broker_id != '' AND status IN ('submitted', 'working', 'partial')
                   ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_remote(self, order_id: int, remote: dict) -> dict:
        status = _map_status(str(remote.get("status", "")))
        filled = remote.get("filled_qty", 0) or 0
        with self._connect() as conn:
            conn.execute(
                "UPDATE watch_orders SET status = ?, filled_qty = ? WHERE id = ?",
                (status, float(filled), order_id),
            )
            row = conn.execute(
                "SELECT id, symbol, status, filled_qty, broker_id FROM watch_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        return {"id": row[0], "symbol": row[1], "status": row[2], "filled_qty": row[3], "broker_id": row[4]}

    def latest(self, symbol: str) -> Optional[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT id, symbol, session_date, status, broker_id, filled_qty, qty
                   FROM watch_orders WHERE symbol = ? ORDER BY id DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        return None if row is None else dict(row)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS watch_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    status TEXT NOT NULL,
                    broker_id TEXT NOT NULL DEFAULT '',
                    filled_qty REAL NOT NULL DEFAULT 0,
                    limit_price REAL
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


def _map_status(remote: str) -> str:
    text = remote.lower()
    if text in {"partially_filled", "partial"}:
        return "partial"
    if text == "filled":
        return "filled"
    if text in {"rejected", "canceled", "cancelled", "expired"}:
        return "rejected" if text == "rejected" else "canceled" if text in {"canceled", "cancelled"} else "expired"
    if text in {"new", "accepted", "pending_new", "accepted_for_bidding"}:
        return "working"
    return "working"

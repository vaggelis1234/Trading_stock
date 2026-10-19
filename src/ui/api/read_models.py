"""JSON views over an already written Universe snapshot database.

The scan, the score, and the rank are whatever ``run_universe`` stored.
Nothing here calls the pipeline, the scorer, or ``BarStore``.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional, Sequence

from src.persistence.store import SNAPSHOT_CURRENT, _parse_snapshot
from src.ui.api.db import connect_readonly
from src.ui.universe_view import (
    METRIC_FIELDS,
    TOP_N,
    display_status,
    history_start,
    metric_coverage,
    rank_movement,
    reason_text,
)


class NoSnapshots(Exception):
    """The database has no operational scan to show."""


class SymbolNotFound(Exception):
    """The latest operational scan does not contain this symbol."""


def latest_scan_payload(path: str) -> dict[str, Any]:
    with connect_readonly(path) as connection:
        resolved, rows, previous = _operational_scan(connection)
        return _scan_payload(resolved, rows, previous)


def search_symbols(
    path: str,
    *,
    q: Optional[str],
    status: Optional[str],
    min_score: Optional[float],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    with connect_readonly(path) as connection:
        resolved, rows, previous = _operational_scan(connection)
        matched = [
            row
            for row in rows
            if _matches(row, q=q, status=status, min_score=min_score)
        ]
        matched.sort(key=_sort_key)
        page = matched[offset : offset + limit]
        return {
            "scan_timestamp": resolved["scan_timestamp"],
            "run_id": resolved["run_id"],
            "snapshot_kind": resolved["snapshot_kind"],
            "total": len(matched),
            "limit": limit,
            "offset": offset,
            "rows": [_public_row(row, previous) for row in page],
        }


def symbol_payload(path: str, symbol: str, range_key: str) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    with connect_readonly(path) as connection:
        resolved, rows, previous = _operational_scan(connection)
        current = next((row for row in rows if row.get("symbol") == symbol), None)
        if current is None:
            raise SymbolNotFound(symbol)
        since = history_start(current["scan_timestamp"], range_key)
        history = _symbol_history(connection, symbol, since)
        coverage = {
            name: metric_coverage(history, name, current["scan_timestamp"], range_key)
            for name in METRIC_FIELDS
        }
        enrichment = _enrichment(connection, current["scan_timestamp"], symbol)
        movement = _movement(current.get("rank"), previous.get(symbol))
        rank = current.get("rank")
        return {
            "symbol": symbol,
            "scan_timestamp": current["scan_timestamp"],
            "executed_at": current.get("executed_at"),
            "run_id": current.get("run_id"),
            "snapshot_kind": current.get("snapshot_kind"),
            "range": range_key,
            "since": since,
            "detail": {
                "rank": None if rank is None else int(rank),
                **movement,
                "score": _number(current.get("final_score")),
                "status": display_status(current.get("veto_status")),
                "reasons": [reason_text(code) for code in (current.get("veto_reasons") or [])],
                "veto_reasons": list(current.get("veto_reasons") or []),
                "price": _number(current.get("hourly_close")),
                "rsi14": _number(current.get("rsi14")),
                "macd_hist": _number(current.get("macd_hist")),
                "atr_percent": _atr_points(current.get("atr_percent")),
                "volume_ratio": _number(current.get("volume_ratio")),
                "adv20": _number(current.get("adv20")),
                "earnings_check": current.get("earnings_check"),
            },
            "enrichment": enrichment,
            "history": [_history_row(row) for row in history],
            "coverage": coverage,
        }


def _operational_scan(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Latest decision scan, then its rows and the previous decision's ranks.

    A ``CURRENT`` row with a ``run_id`` is an operational decision. ``LOOKBACK``
    rows from the same or a later execution are not, even when their
    ``scan_timestamp`` is greater. When the database has no such decision,
    rows stored before run identity (both fields NULL, or a schema without
    those columns) still count. A database that only has ``LOOKBACK`` rows
    has no operational scan.
    """
    if not _has_table(connection, "universe_snapshots"):
        raise NoSnapshots()
    columns = _column_names(connection, "universe_snapshots")
    resolved = _resolve_scan(connection, columns)
    if resolved is None:
        raise NoSnapshots()
    rows = [_parse_snapshot(row) for row in _fetch_scan_rows(connection, resolved)]
    if not rows:
        raise NoSnapshots()
    previous = _previous_ranks(connection, resolved)
    return resolved, rows, previous


def _resolve_scan(connection: sqlite3.Connection, columns: set[str]) -> Optional[dict[str, Any]]:
    has_identity = "run_id" in columns and "snapshot_kind" in columns
    if has_identity:
        current = connection.execute(
            """SELECT run_id, scan_timestamp
               FROM universe_snapshots
               WHERE snapshot_kind = ?
                 AND run_id IS NOT NULL
               ORDER BY scan_timestamp DESC
               LIMIT 1""",
            (SNAPSHOT_CURRENT,),
        ).fetchone()
        if current is not None:
            return {
                "mode": "current",
                "scan_timestamp": current["scan_timestamp"],
                "run_id": current["run_id"],
                "snapshot_kind": SNAPSHOT_CURRENT,
                "has_identity_columns": True,
            }
        legacy = connection.execute(
            """SELECT MAX(scan_timestamp) AS scan_timestamp
               FROM universe_snapshots
               WHERE run_id IS NULL
                 AND snapshot_kind IS NULL"""
        ).fetchone()
        if legacy is None or legacy["scan_timestamp"] is None:
            return None
        return {
            "mode": "legacy",
            "scan_timestamp": legacy["scan_timestamp"],
            "run_id": None,
            "snapshot_kind": None,
            "has_identity_columns": True,
        }
    legacy = connection.execute(
        "SELECT MAX(scan_timestamp) AS scan_timestamp FROM universe_snapshots"
    ).fetchone()
    if legacy is None or legacy["scan_timestamp"] is None:
        return None
    return {
        "mode": "legacy",
        "scan_timestamp": legacy["scan_timestamp"],
        "run_id": None,
        "snapshot_kind": None,
        "has_identity_columns": False,
    }


def _fetch_scan_rows(connection: sqlite3.Connection, resolved: Mapping[str, Any]) -> Sequence[sqlite3.Row]:
    if resolved["mode"] == "current":
        return connection.execute(
            """SELECT * FROM universe_snapshots
               WHERE scan_timestamp = ?
                 AND run_id = ?
                 AND snapshot_kind = ?
               ORDER BY (rank IS NULL), rank, symbol""",
            (resolved["scan_timestamp"], resolved["run_id"], SNAPSHOT_CURRENT),
        ).fetchall()
    if resolved["has_identity_columns"]:
        return connection.execute(
            """SELECT * FROM universe_snapshots
               WHERE scan_timestamp = ?
                 AND run_id IS NULL
                 AND snapshot_kind IS NULL
               ORDER BY (rank IS NULL), rank, symbol""",
            (resolved["scan_timestamp"],),
        ).fetchall()
    return connection.execute(
        """SELECT * FROM universe_snapshots
           WHERE scan_timestamp = ?
           ORDER BY (rank IS NULL), rank, symbol""",
        (resolved["scan_timestamp"],),
    ).fetchall()


def _previous_ranks(connection: sqlite3.Connection, resolved: Mapping[str, Any]) -> dict[str, int]:
    """Same rank lookup as ``load_previous_ranks``, on the caller's connection.

    Identified scans use the previous ``CURRENT`` decision. Legacy scans use
    the latest earlier ranked row. ``LOOKBACK`` is not a decision.
    """
    scan_timestamp = resolved["scan_timestamp"]
    if resolved["mode"] == "legacy":
        rows = connection.execute(
            """SELECT u.symbol AS symbol, u.rank AS rank
               FROM universe_snapshots u
               JOIN (
                   SELECT symbol, MAX(scan_timestamp) AS scan_timestamp
                   FROM universe_snapshots
                   WHERE scan_timestamp < ? AND rank IS NOT NULL
                   GROUP BY symbol
               ) prev
                 ON u.symbol = prev.symbol
                AND u.scan_timestamp = prev.scan_timestamp""",
            (scan_timestamp,),
        ).fetchall()
        return {row["symbol"]: int(row["rank"]) for row in rows}
    previous = connection.execute(
        """SELECT run_id, scan_timestamp
           FROM universe_snapshots
           WHERE snapshot_kind = ?
             AND run_id IS NOT NULL
             AND scan_timestamp < ?
           ORDER BY scan_timestamp DESC
           LIMIT 1""",
        (SNAPSHOT_CURRENT, scan_timestamp),
    ).fetchone()
    if previous is None:
        return {}
    rows = connection.execute(
        """SELECT symbol, rank
           FROM universe_snapshots
           WHERE scan_timestamp = ?
             AND run_id = ?
             AND snapshot_kind = ?
             AND rank IS NOT NULL""",
        (previous["scan_timestamp"], previous["run_id"], SNAPSHOT_CURRENT),
    ).fetchall()
    return {row["symbol"]: int(row["rank"]) for row in rows}


def _symbol_history(connection: sqlite3.Connection, symbol: str, since: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT * FROM universe_snapshots
           WHERE symbol = ? AND scan_timestamp >= ?
           ORDER BY scan_timestamp ASC""",
        (symbol, since),
    ).fetchall()
    return [_parse_snapshot(row) for row in rows]


def _enrichment(
    connection: sqlite3.Connection,
    scan_timestamp: str,
    symbol: str,
) -> Optional[dict[str, Any]]:
    if not _has_table(connection, "minute_enrichment"):
        return None
    row = connection.execute(
        """SELECT * FROM minute_enrichment
           WHERE scan_timestamp = ? AND symbol = ?""",
        (scan_timestamp, symbol),
    ).fetchone()
    if row is None:
        return None
    return {name: row[name] for name in row.keys()}


def _scan_payload(
    resolved: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    previous: Mapping[str, int],
) -> dict[str, Any]:
    ranked = [row for row in rows if _in_top(row)]
    ranked.sort(key=lambda row: (int(row["rank"]), str(row.get("symbol") or "")))
    earnings = {row.get("earnings_check") for row in rows}
    passed = sum(1 for row in rows if row.get("veto_status") == "PASS")
    return {
        "scan_timestamp": resolved["scan_timestamp"],
        "executed_at": rows[0].get("executed_at"),
        "run_id": resolved["run_id"],
        "snapshot_kind": resolved["snapshot_kind"],
        "evaluated": len(rows),
        "scored": sum(1 for row in rows if row.get("final_score") is not None),
        "passed": passed,
        "blocked": len(rows) - passed,
        "earnings_pending": earnings == {"unavailable"},
        "rows": [_public_row(row, previous) for row in ranked[:TOP_N]],
    }


def _public_row(row: Mapping[str, Any], previous: Mapping[str, int]) -> dict[str, Any]:
    rank = row.get("rank")
    symbol = row.get("symbol")
    previous_rank = None if rank is None or symbol is None else previous.get(symbol)
    return {
        "rank": None if rank is None else int(rank),
        "symbol": symbol,
        "price": _number(row.get("hourly_close")),
        "status": display_status(row.get("veto_status")),
        "score": _number(row.get("final_score")),
        **_movement(rank, previous_rank),
        "rsi14": _number(row.get("rsi14")),
        "macd_hist": _number(row.get("macd_hist")),
        "atr_percent": _atr_points(row.get("atr_percent")),
        "volume_ratio": _number(row.get("volume_ratio")),
        "adv20": _number(row.get("adv20")),
        "veto_reasons": list(row.get("veto_reasons") or []),
    }


def _history_row(row: Mapping[str, Any]) -> dict[str, Any]:
    reasons = list(row.get("veto_reasons") or [])
    return {
        "scan_timestamp": row.get("scan_timestamp"),
        "run_id": row.get("run_id"),
        "snapshot_kind": row.get("snapshot_kind"),
        "status": display_status(row.get("veto_status")),
        "reasons": [reason_text(code) for code in reasons],
        "veto_reasons": reasons,
        "rsi14": _number(row.get("rsi14")),
        "final_score": _number(row.get("final_score")),
        "atr_percent": _atr_points(row.get("atr_percent")),
        "volume_ratio": _number(row.get("volume_ratio")),
    }


def _movement(rank, previous_rank) -> dict[str, Any]:
    if rank is None:
        return {"movement": None, "movement_label": None, "previous_rank": None}
    if previous_rank is None:
        return {"movement": None, "movement_label": "new", "previous_rank": None}
    delta, _label = rank_movement(int(rank), int(previous_rank))
    if delta is None:
        label = "new"
    elif delta > 0:
        label = "up"
    elif delta < 0:
        label = "down"
    else:
        label = "flat"
    return {
        "movement": delta,
        "movement_label": label,
        "previous_rank": int(previous_rank),
    }


def _matches(row: Mapping[str, Any], *, q: Optional[str], status: Optional[str], min_score: Optional[float]) -> bool:
    if q and q.upper() not in str(row.get("symbol") or "").upper():
        return False
    if status == "PASS" and row.get("veto_status") != "PASS":
        return False
    if status == "BLOCKED" and row.get("veto_status") == "PASS":
        return False
    if min_score is not None:
        score = row.get("final_score")
        if score is None or float(score) < min_score:
            return False
    return True


def _sort_key(row: Mapping[str, Any]) -> tuple:
    rank = row.get("rank")
    missing = rank is None
    return (missing, 0 if missing else int(rank), str(row.get("symbol") or ""))


def _in_top(row: Mapping[str, Any]) -> bool:
    rank = row.get("rank")
    if rank is None:
        return False
    try:
        number = int(rank)
    except (TypeError, ValueError):
        return False
    return 1 <= number <= TOP_N


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    if number != number:
        return None
    return number


def _atr_points(value: Any) -> Optional[float]:
    """ATR% in percent points, matching the Streamlit table and history chart."""
    number = _number(value)
    if number is None:
        return None
    return number * 100.0


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in {"universe_snapshots", "minute_enrichment"}:
        raise ValueError(f"unknown table: {table}")
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

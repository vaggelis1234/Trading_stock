import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def init_db(path: str = "trades.db"):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            entry_price REAL,
            exit_price REAL,
            entry_time TEXT,
            exit_time TEXT,
            pnl REAL
        )"""
    )
    conn.commit()
    conn.close()


def save_trades(trades: List[Dict[str, Any]], path: str = "trades.db"):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    for t in trades:
        cur.execute(
            "INSERT INTO trades (symbol, side, qty, entry_price, exit_price, entry_time, exit_time, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.get("symbol"),
                t.get("side"),
                t.get("qty"),
                t.get("entry_price"),
                t.get("exit_price"),
                str(t.get("entry_time")),
                str(t.get("exit_time")),
                t.get("pnl"),
            ),
        )
    conn.commit()
    conn.close()


SNAPSHOT_COLUMNS = [
    "scan_timestamp",
    "symbol",
    "hourly_close",
    "daily_close",
    "daily_ema200",
    "rsi14",
    "macd_hist",
    "atr14",
    "atr_percent",
    "volume",
    "volume_ratio",
    "adv20",
    "veto_status",
    "veto_reasons",
    "earnings_check",
    "trend_score",
    "momentum_score",
    "volatility_score",
    "volume_liquidity_score",
    "final_score",
    "rank",
]


def init_universe_db(path: str = "artifacts/universe.db") -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS universe_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            hourly_close REAL,
            daily_close REAL,
            daily_ema200 REAL,
            rsi14 REAL,
            macd_hist REAL,
            atr14 REAL,
            atr_percent REAL,
            volume REAL,
            volume_ratio REAL,
            adv20 REAL,
            veto_status TEXT NOT NULL,
            veto_reasons TEXT NOT NULL,
            earnings_check TEXT NOT NULL,
            trend_score REAL,
            momentum_score REAL,
            volatility_score REAL,
            volume_liquidity_score REAL,
            final_score REAL,
            rank INTEGER,
            UNIQUE(scan_timestamp, symbol)
        )"""
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS idx_universe_symbol_ts
           ON universe_snapshots(symbol, scan_timestamp)"""
    )
    _ensure_executed_at(cur)
    conn.commit()
    conn.close()


def _ensure_executed_at(cur: sqlite3.Cursor) -> None:
    """Add the scan-execution clock if this database was created without it.

    ``scan_timestamp`` stays the market-data time. ``executed_at`` is when the
    scan that wrote the row actually ran.
    """
    names = {row[1] for row in cur.execute("PRAGMA table_info(universe_snapshots)")}
    if names and "executed_at" not in names:
        cur.execute("ALTER TABLE universe_snapshots ADD COLUMN executed_at TEXT")


def _execution_stamp(executed_at: Optional[str]) -> str:
    if executed_at:
        return executed_at
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _reason_text(reasons: Any) -> str:
    if isinstance(reasons, str):
        return reasons
    return json.dumps(list(reasons or []))


def save_universe_snapshots(
    rows: List[Dict[str, Any]],
    path: str = "artifacts/universe.db",
    executed_at: Optional[str] = None,
) -> int:
    """Insert snapshots. Existing (scan_timestamp, symbol) rows are left unchanged.

    ``executed_at`` is the wall-clock time of this save. It is never copied
    from ``scan_timestamp``. Rows written together share one execution time.
    """
    init_universe_db(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    before = cur.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0]
    stored_columns = SNAPSHOT_COLUMNS + ["executed_at"]
    columns = ", ".join(stored_columns)
    placeholders = ", ".join(["?"] * len(stored_columns))
    execution_stamp = _execution_stamp(executed_at)
    for row in rows:
        values = []
        for column in SNAPSHOT_COLUMNS:
            value = row.get(column)
            if column == "veto_reasons":
                value = _reason_text(value)
            elif column == "rank" and value is not None:
                value = int(value)
            values.append(value)
        values.append(execution_stamp)
        cur.execute(
            f"INSERT OR IGNORE INTO universe_snapshots ({columns}) VALUES ({placeholders})",
            values,
        )
    conn.commit()
    after = cur.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0]
    conn.close()
    return int(after - before)


def _parse_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    parsed = {column: row[column] for column in SNAPSHOT_COLUMNS}
    parsed["executed_at"] = row["executed_at"] if "executed_at" in keys else None
    raw_reasons = parsed.get("veto_reasons")
    if not raw_reasons:
        parsed["veto_reasons"] = []
    else:
        try:
            parsed["veto_reasons"] = json.loads(raw_reasons)
        except json.JSONDecodeError:
            parsed["veto_reasons"] = [part for part in str(raw_reasons).split(",") if part]
    return parsed


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def count_snapshots(path: str, symbol: Optional[str] = None) -> int:
    if not os.path.exists(path):
        return 0
    conn = _connect(path)
    if symbol is None:
        count = conn.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM universe_snapshots WHERE symbol = ?",
            (symbol,),
        ).fetchone()[0]
    conn.close()
    return int(count)


def load_latest_scan(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    conn = _connect(path)
    latest = conn.execute("SELECT MAX(scan_timestamp) FROM universe_snapshots").fetchone()[0]
    if latest is None:
        conn.close()
        return []
    rows = conn.execute(
        """SELECT * FROM universe_snapshots
           WHERE scan_timestamp = ?
           ORDER BY (rank IS NULL), rank, symbol""",
        (latest,),
    ).fetchall()
    conn.close()
    return [_parse_snapshot(row) for row in rows]


def load_symbol_history(
    path: str,
    symbol: str,
    limit: int = 24,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Snapshots for ``symbol``, oldest first.

    ``since`` keeps rows at or after that timestamp and ignores ``limit``.
    Without ``since``, the most recent ``limit`` rows are returned.
    """
    if not os.path.exists(path):
        return []
    conn = _connect(path)
    if since is None:
        rows = conn.execute(
            """SELECT * FROM universe_snapshots
               WHERE symbol = ?
               ORDER BY scan_timestamp DESC
               LIMIT ?""",
            (symbol, int(limit)),
        ).fetchall()
        conn.close()
        history = [_parse_snapshot(row) for row in rows]
        history.reverse()
        return history
    rows = conn.execute(
        """SELECT * FROM universe_snapshots
           WHERE symbol = ? AND scan_timestamp >= ?
           ORDER BY scan_timestamp ASC""",
        (symbol, since),
    ).fetchall()
    conn.close()
    return [_parse_snapshot(row) for row in rows]


def load_previous_ranks(path: str, before_timestamp: str) -> Dict[str, Optional[int]]:
    """Latest earlier rank for each symbol.

    The lookup walks back to the most recent snapshot before
    ``before_timestamp`` that already has a rank. Symbols with no earlier
    rank are omitted. Callers treat a missing symbol as no previous rank.
    """
    if not os.path.exists(path):
        return {}
    conn = _connect(path)
    rows = conn.execute(
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
        (before_timestamp,),
    ).fetchall()
    conn.close()
    return {row["symbol"]: int(row["rank"]) for row in rows}


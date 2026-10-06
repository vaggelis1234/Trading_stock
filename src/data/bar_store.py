"""Persistent raw OHLCV cache for Universe v1.

Snapshots stay in the universe database. This store keeps one normalized bar
per ``(symbol, interval, bar_timestamp)`` and a coverage row that says which
window was successfully checked. Freshness uses that coverage row, not the
minimum or maximum stored timestamp: a closed market can legitimately have
no bar.
"""

import logging
import os
import sqlite3
from typing import Optional

import pandas as pd

from src.data.provider import MarketDataUnavailable, prepare_ohlcv
from src.data.session_bars import HourlySession, classify_hourly_session

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
SESSION_COLUMNS = (
    "session_type",
    "observed_minutes",
    "expected_minutes",
    "coverage_ratio",
)
_SESSION_COLUMN_SQL = {
    "session_type": "TEXT",
    "observed_minutes": "INTEGER",
    "expected_minutes": "INTEGER",
    "coverage_ratio": "REAL",
}
TAIL_CALENDAR_DAYS = 7
HOURLY_OVERLAP = pd.Timedelta(hours=2)
SESSION_CLOSE_HOUR = 16

_BAR_UPSERT = """
INSERT INTO market_bars (
    symbol, interval, bar_timestamp, open, high, low, close, volume
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, interval, bar_timestamp) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume
"""

_COVERAGE_UPSERT = """
INSERT INTO market_coverage (
    symbol, interval, window_start, checked_through, latest_bar
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(symbol, interval) DO UPDATE SET
    window_start = excluded.window_start,
    checked_through = excluded.checked_through,
    latest_bar = excluded.latest_bar
"""


def _ensure_hourly_session_columns(conn: sqlite3.Connection) -> None:
    """Add hourly session facts to a market_bars table created without them.

    ``market_coverage`` is the fetch window (what was checked). These columns
    are per-bar minute presence. They stay on ``market_bars`` and are not
    written into ``market_coverage``.
    """
    names = {row[1] for row in conn.execute("PRAGMA table_info(market_bars)")}
    for name, declaration in _SESSION_COLUMN_SQL.items():
        if names and name not in names:
            conn.execute(f"ALTER TABLE market_bars ADD COLUMN {name} {declaration}")


def bars_db_path(path: Optional[str] = None) -> str:
    if path:
        return path
    return os.getenv("MARKET_BARS_DB", os.path.join("artifacts", "market_bars.db"))


def _as_utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _utc_iso(value) -> str:
    return _as_utc(value).floor("min").isoformat()


def _number(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def _materially_different(stored: sqlite3.Row, row: pd.Series) -> bool:
    for column in OHLCV_COLUMNS:
        left = stored[column]
        right = _number(row[column])
        if left is None or right is None:
            if left != right:
                return True
            continue
        if not pd.isna(left) and abs(float(left) - right) <= max(1e-4, abs(right) * 1e-8):
            continue
        return True
    return False


def completed_session_close(clock) -> pd.Timestamp:
    """UTC timestamp when the latest finished New York session closed.

    Weekends walk back to Friday. A weekday before 16:00 still belongs to the
    previous completed session, so the in-progress session is not required.
    """
    et = _as_utc(clock).tz_convert("America/New_York")
    session = et.normalize()
    if et.weekday() >= 5 or et.hour < SESSION_CLOSE_HOUR:
        session = session - pd.Timedelta(days=1)
        while session.weekday() >= 5:
            session = session - pd.Timedelta(days=1)
    close = session + pd.Timedelta(hours=SESSION_CLOSE_HOUR)
    return close.tz_convert("UTC")


def _completed_overlap_bar(interval: str, bar_ts: pd.Timestamp, clock, newest: pd.Timestamp) -> bool:
    """True when a changed bar is historical rather than still forming."""
    bar_ts = _as_utc(bar_ts)
    if interval == "1h":
        return bar_ts <= _as_utc(newest) - HOURLY_OVERLAP
    session_start = _as_utc(clock).tz_convert("America/New_York").normalize()
    return bar_ts.tz_convert("America/New_York").normalize() < session_start


class BarStore:
    def __init__(self, path: Optional[str] = None):
        self.path = bars_db_path(path)
        self.actions = {
            "initialized": 0,
            "backfilled": 0,
            "refreshed": 0,
            "skipped": 0,
            "failed": 0,
            "rebuilt": 0,
        }
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS market_bars (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    bar_timestamp TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    session_type TEXT,
                    observed_minutes INTEGER,
                    expected_minutes INTEGER,
                    coverage_ratio REAL,
                    PRIMARY KEY (symbol, interval, bar_timestamp)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS market_coverage (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    checked_through TEXT NOT NULL,
                    latest_bar TEXT,
                    PRIMARY KEY (symbol, interval)
                )"""
            )
            _ensure_hourly_session_columns(conn)
            conn.commit()
        finally:
            conn.close()

    def update_hourly_session(self, symbol: str, bar_timestamp, minute_bars: Optional[pd.DataFrame] = None) -> bool:
        """Store session facts on an existing 1h row.

        Does not insert a bar. A weekend, holiday, or other missing hour stays
        absent rather than becoming a zero row. ``minute_bars=None`` keeps
        ``observed_minutes`` and ``coverage_ratio`` NULL. An invalid timestamp
        is not given a session type. OHLCV columns are left unchanged.
        ``market_coverage`` is not updated.
        """
        session = classify_hourly_session(bar_timestamp, minute_bars)
        if not session.valid:
            return False
        conn = self._connect()
        try:
            cursor = conn.execute(
                """UPDATE market_bars
                   SET session_type = ?,
                       observed_minutes = ?,
                       expected_minutes = ?,
                       coverage_ratio = ?
                   WHERE symbol = ? AND interval = '1h' AND bar_timestamp = ?""",
                (
                    session.session_type,
                    session.observed_minutes,
                    session.expected_minutes,
                    session.coverage_ratio,
                    symbol,
                    _utc_iso(bar_timestamp),
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def hourly_session(self, symbol: str, bar_timestamp) -> Optional[HourlySession]:
        """Session facts stored for one 1h bar, or ``None`` when the row is absent."""
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT session_type, observed_minutes, expected_minutes, coverage_ratio
                   FROM market_bars
                   WHERE symbol = ? AND interval = '1h' AND bar_timestamp = ?""",
                (symbol, _utc_iso(bar_timestamp)),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        session_type = row["session_type"]
        observed = row["observed_minutes"]
        expected = row["expected_minutes"]
        ratio = row["coverage_ratio"]
        valid = session_type is not None and expected is not None
        return HourlySession(
            valid=valid,
            session_type=session_type,
            expected_minutes=None if expected is None else int(expected),
            observed_minutes=None if observed is None else int(observed),
            coverage_ratio=None if ratio is None else float(ratio),
        )

    def coverage(self, symbol: str, interval: str):
        conn = self._connect()
        try:
            return conn.execute(
                """SELECT window_start, checked_through, latest_bar
                   FROM market_coverage
                   WHERE symbol = ? AND interval = ?""",
                (symbol, interval),
            ).fetchone()
        finally:
            conn.close()

    def sync(
        self,
        provider,
        symbol: str,
        interval: str,
        required_start: str,
        required_end: str,
        clock,
    ) -> str:
        """Bring one series up to ``clock``. Provider failures leave the store unchanged.

        ``MarketDataUnavailable`` is the source itself rejecting the scan.
        It is not counted as a per-symbol failure.
        """
        try:
            action = self._sync(provider, symbol, interval, required_start, required_end, clock)
        except MarketDataUnavailable:
            raise
        except Exception:
            logger.exception("bar sync failed %s %s", symbol, interval)
            action = "failed"
        self.actions[action] = self.actions.get(action, 0) + 1
        if action == "skipped":
            logger.debug("bar sync skipped %s %s", symbol, interval)
        else:
            logger.info("bar sync %s %s %s", action, symbol, interval)
        return action

    def _sync(self, provider, symbol, interval, required_start, required_end, clock) -> str:
        state = self.coverage(symbol, interval)
        if state is None:
            frame = self._fetch(provider, symbol, interval, required_start, required_end)
            if frame is None:
                return "failed"
            self._persist(symbol, interval, frame, required_start, clock, replace_all=False)
            return "initialized"

        if state["window_start"] > required_start:
            frame = self._fetch(provider, symbol, interval, required_start, required_end)
            if frame is None:
                return "failed"
            return self._store_fetched(
                provider, symbol, interval, frame, required_start, required_end, clock, full_window=True, action="backfilled"
            )

        if interval == "1d" and _as_utc(state["checked_through"]) >= completed_session_close(clock):
            return "skipped"
        if interval == "1h" and _as_utc(state["checked_through"]) >= _as_utc(clock).floor("min"):
            return "skipped"

        request_start = self._tail_start(interval, required_start, state)
        frame = self._fetch(provider, symbol, interval, request_start, required_end)
        if frame is None:
            return "failed"
        return self._store_fetched(
            provider, symbol, interval, frame, required_start, required_end, clock, full_window=False, action="refreshed"
        )

    def _store_fetched(self, provider, symbol, interval, frame, required_start, required_end, clock, full_window: bool, action: str) -> str:
        if self._revision_detected(symbol, interval, frame, clock):
            if not full_window:
                frame = self._fetch(provider, symbol, interval, required_start, required_end)
                if frame is None:
                    return "failed"
            self._persist(symbol, interval, frame, required_start, clock, replace_all=True)
            return "rebuilt"
        self._persist(symbol, interval, frame, required_start, clock, replace_all=False)
        return action

    def _fetch(self, provider, symbol: str, interval: str, start: str, end: str):
        try:
            raw = provider.get_bars(symbol, start, end, interval)
        except MarketDataUnavailable:
            raise
        except Exception:
            logger.exception("bar sync provider failed %s %s", symbol, interval)
            return None
        return prepare_ohlcv(raw)

    def _tail_start(self, interval: str, required_start: str, state) -> str:
        anchor = state["latest_bar"] or state["checked_through"]
        anchor_ts = _as_utc(anchor)
        if interval == "1h":
            anchor_ts = anchor_ts - HOURLY_OVERLAP
        start_ts = anchor_ts - pd.Timedelta(days=TAIL_CALENDAR_DAYS - 1)
        start_text = start_ts.strftime("%Y-%m-%d")
        if start_text < required_start:
            return required_start
        return start_text

    def _revision_detected(self, symbol: str, interval: str, frame: pd.DataFrame, clock) -> bool:
        if frame is None or frame.empty:
            return False
        newest = frame.index.max()
        start_iso = _utc_iso(frame.index.min())
        end_iso = _utc_iso(newest)
        conn = self._connect()
        try:
            stored = {
                row["bar_timestamp"]: row
                for row in conn.execute(
                    """SELECT * FROM market_bars
                       WHERE symbol = ? AND interval = ?
                         AND bar_timestamp >= ? AND bar_timestamp <= ?""",
                    (symbol, interval, start_iso, end_iso),
                )
            }
        finally:
            conn.close()
        for ts, row in frame.iterrows():
            iso = _utc_iso(ts)
            previous = stored.get(iso)
            if previous is None:
                continue
            if not _completed_overlap_bar(interval, ts, clock, newest):
                continue
            if _materially_different(previous, row):
                return True
        return False

    def _persist(self, symbol, interval, frame, required_start, clock, replace_all: bool) -> None:
        rows = []
        if frame is not None and not frame.empty:
            for ts, row in frame.iterrows():
                rows.append(
                    (
                        symbol,
                        interval,
                        _utc_iso(ts),
                        _number(row["open"]),
                        _number(row["high"]),
                        _number(row["low"]),
                        _number(row["close"]),
                        _number(row["volume"]),
                    )
                )
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            saved_session = {}
            if replace_all:
                saved_session = {
                    row["bar_timestamp"]: tuple(row[column] for column in SESSION_COLUMNS)
                    for row in conn.execute(
                        f"""SELECT bar_timestamp, {", ".join(SESSION_COLUMNS)}
                            FROM market_bars
                            WHERE symbol = ? AND interval = ?""",
                        (symbol, interval),
                    )
                    if any(row[column] is not None for column in SESSION_COLUMNS)
                }
                conn.execute(
                    "DELETE FROM market_bars WHERE symbol = ? AND interval = ?",
                    (symbol, interval),
                )
            if rows:
                conn.executemany(_BAR_UPSERT, rows)
            for iso, values in saved_session.items():
                conn.execute(
                    """UPDATE market_bars
                       SET session_type = ?,
                           observed_minutes = ?,
                           expected_minutes = ?,
                           coverage_ratio = ?
                       WHERE symbol = ? AND interval = ? AND bar_timestamp = ?""",
                    (*values, symbol, interval, iso),
                )
            existing = conn.execute(
                """SELECT window_start, latest_bar FROM market_coverage
                   WHERE symbol = ? AND interval = ?""",
                (symbol, interval),
            ).fetchone()
            if replace_all or existing is None:
                window_start = required_start
                latest = rows[-1][2] if rows else None
                if rows:
                    latest = max(row[2] for row in rows)
            else:
                window_start = min(existing["window_start"], required_start)
                candidates = [row[2] for row in rows]
                if existing["latest_bar"]:
                    candidates.append(existing["latest_bar"])
                latest = max(candidates) if candidates else None
            conn.execute(
                _COVERAGE_UPSERT,
                (symbol, interval, window_start, _utc_iso(clock), latest),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_window(self, symbol: str, interval: str, required_start: str, required_end: str) -> pd.DataFrame:
        """Bars inside the evaluation window only.

        ``required_start`` is inclusive and ``required_end`` is exclusive, as
        date strings. Older cached bars stay in the file and out of EMA200.
        """
        conn = self._connect()
        try:
            found = conn.execute(
                """SELECT bar_timestamp, open, high, low, close, volume
                   FROM market_bars
                   WHERE symbol = ? AND interval = ?
                     AND bar_timestamp >= ? AND bar_timestamp < ?
                   ORDER BY bar_timestamp""",
                (symbol, interval, required_start, required_end),
            ).fetchall()
        finally:
            conn.close()
        if not found:
            return prepare_ohlcv(None)
        index = pd.DatetimeIndex(pd.to_datetime([row["bar_timestamp"] for row in found], utc=True))
        frame = pd.DataFrame(
            {column: [row[column] for row in found] for column in OHLCV_COLUMNS},
            index=index,
        )
        return prepare_ohlcv(frame)

    def sync_and_load(self, provider, symbol, interval, required_start, required_end, clock) -> pd.DataFrame:
        self.sync(provider, symbol, interval, required_start, required_end, clock)
        return self.load_window(symbol, interval, required_start, required_end)

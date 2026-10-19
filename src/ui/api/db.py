"""SQLite connections that cannot create or modify a Universe database."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


READ_TIMEOUT_SECONDS = 5.0


def universe_db_path() -> str:
    """Snapshot database shared with the scan and the Streamlit viewer.

    ``UNIVERSE_DB`` wins. Otherwise the path is ``artifacts/universe.db``,
    the same default as ``scripts/run_universe.py``.
    """
    override = os.getenv("UNIVERSE_DB", "").strip()
    if override:
        return override
    return os.path.join("artifacts", "universe.db")


@contextmanager
def connect_readonly(path: str) -> Iterator[sqlite3.Connection]:
    """Open an existing database in SQLite read-only mode.

    A missing file is not created. The connection does not run schema
    initialization. ``timeout`` is the SQLite busy wait, in seconds.
    Closing the connection does not commit a write.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()

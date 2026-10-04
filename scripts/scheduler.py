"""Hourly Universe v1 scan.

Persists snapshots and exports the latest ranking. This job does not place orders.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apscheduler.schedulers.blocking import BlockingScheduler

from src.data.scan import build_sp500_universe
from src.universe.pipeline import run_universe

ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "artifacts")
DB_PATH = os.getenv("UNIVERSE_DB", os.path.join(ARTIFACT_DIR, "universe.db"))


def resolve_symbols():
    raw = os.getenv("UNIVERSE_SYMBOLS", "").strip()
    if raw:
        return [part.strip().upper() for part in raw.split(",") if part.strip()]
    raw_size = os.getenv("UNIVERSE_SIZE", "").strip()
    limit = int(raw_size) if raw_size else None
    return build_sp500_universe(limit=limit)


def job():
    symbols = resolve_symbols()
    run_universe(symbols, db_path=DB_PATH, artifact_dir=ARTIFACT_DIR)


def start():
    sched = BlockingScheduler()
    sched.add_job(job, "cron", minute=0)
    print("Scheduler started — Universe v1 scan runs hourly at minute 0")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    job()

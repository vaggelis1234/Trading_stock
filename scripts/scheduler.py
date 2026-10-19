"""Universe session scheduler.

Scans run at the New York cash-session clock, not at minute 0 and not at
process start. ``job`` remains an explicit manual scan. Starting this module
does not call it.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.scan import build_sp500_universe

ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "artifacts")
DB_PATH = os.getenv("UNIVERSE_DB", os.path.join(ARTIFACT_DIR, "universe.db"))
RUNTIME_DB = os.getenv("RUNTIME_DB", os.path.join(ARTIFACT_DIR, "runtime.db"))


def resolve_symbols():
    raw = os.getenv("UNIVERSE_SYMBOLS", "").strip()
    if raw:
        return [part.strip().upper() for part in raw.split(",") if part.strip()]
    raw_size = os.getenv("UNIVERSE_SIZE", "").strip()
    limit = int(raw_size) if raw_size else None
    return build_sp500_universe(limit=limit)


def job():
    """Manual Universe scan. Not used by the session clock."""
    from src.universe.pipeline import run_universe

    symbols = resolve_symbols()
    run_universe(symbols, db_path=DB_PATH, artifact_dir=ARTIFACT_DIR)


def build_runner():
    """Wire the session clock, the scheduled scan, and the watch engine."""
    import pandas as pd

    from src.execution.alpaca_adapter import AlpacaAdapter
    from src.market.calendar import NyseCalendar
    from src.risk.manager import RiskManager
    from src.universe.schedule_store import ScheduleStore
    from src.universe.scheduled_scan import run_scheduled_universe
    from src.universe.session_runner import SessionRunner
    from src.watch.engine import WatchEngine
    from src.watch.feed import IexFeed
    from src.watch.orders import OrderStore

    calendar = NyseCalendar()
    orders = OrderStore(RUNTIME_DB)
    adapter = AlpacaAdapter()
    feed = IexFeed()
    watch = WatchEngine(orders, adapter, RiskManager(cash=100000), feed)
    store = ScheduleStore(RUNTIME_DB)

    def scan(slot, now):
        del now
        return run_scheduled_universe(
            resolve_symbols(),
            slot,
            calendar=calendar,
            db_path=DB_PATH,
            artifact_dir=ARTIFACT_DIR,
            now=pd.Timestamp.now(tz="America/New_York"),
        )

    return SessionRunner(
        calendar=calendar,
        schedule_store=store,
        watch=watch,
        scan_fn=scan,
        universe_db=DB_PATH,
    )


def start():
    """Run the New York session clock. Does not scan before the first due slot."""
    import pandas as pd
    from apscheduler.schedulers.blocking import BlockingScheduler

    runner = build_runner()
    runner.tick(pd.Timestamp.now(tz="America/New_York"))

    def _tick():
        runner.tick(pd.Timestamp.now(tz="America/New_York"))

    sched = BlockingScheduler(timezone="America/New_York")
    sched.add_job(
        _tick,
        "cron",
        day_of_week="mon-fri",
        hour="10-16",
        minute="*",
        timezone="America/New_York",
        misfire_grace_time=30,
        coalesce=True,
        max_instances=1,
    )
    print("Scheduler started — Universe scans follow the America/New_York cash session")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    start()

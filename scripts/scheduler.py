"""Universe session scheduler.

Scans run at the New York cash-session clock, not at minute 0 and not at
process start. ``job`` is the manual start. When a calendar slot is inside
its five-minute grace window, that call claims the slot. A second call in the
same window does not scan again. Starting this module does not call ``job``.

The process still wakes every minute so a slot is not missed. The watch
evaluates that minute only while candidates or working orders exist. The
final slot and an early close do not open buys; the watch reads the session
close from the NYSE calendar.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

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


def run_standalone_universe(stop_check=None, progress=None):
    """One immediate Universe snapshot. Does not claim a scheduled slot."""
    from src.universe.pipeline import run_universe

    return run_universe(
        resolve_symbols(),
        db_path=DB_PATH,
        artifact_dir=ARTIFACT_DIR,
        stop_check=stop_check,
        progress=progress,
    )


def job():
    """Manual Universe start. A due slot is claimed so the clock does not repeat it."""
    import pandas as pd

    from src.control.instance import AlreadyRunning, InstanceLock
    from src.universe.session_runner import manual_scan_action

    lock = InstanceLock()
    try:
        lock.acquire()
    except AlreadyRunning:
        logger.error("manual scan refused; Trading Bot is already running")
        return
    try:
        now = pd.Timestamp.now(tz="America/New_York")
        runner = build_runner()
        session = runner.calendar.session(now.date())
        known = runner.store.known(session.day) if session is not None else set()
        action = manual_scan_action(now, session, known)
        if action == "due_slot":
            runner.tick(now)
            return
        if action == "already_recorded":
            logger.info("manual scan skipped; slot at %s is already recorded", now.isoformat())
            return
        run_standalone_universe()
    finally:
        lock.release()


def build_runner(*, enable_watch: bool = True, scan_fn=None):
    """Wire the session clock, the scheduled scan, and the optional watch engine."""
    from src.market.calendar import NyseCalendar
    from src.universe.schedule_store import ScheduleStore
    from src.universe.scheduled_scan import run_scheduled_universe
    from src.universe.session_runner import SessionRunner

    calendar = NyseCalendar()
    watch = _build_watch() if enable_watch else None
    store = ScheduleStore(RUNTIME_DB)

    def scheduled(slot, now):
        return run_scheduled_universe(
            resolve_symbols(),
            slot,
            calendar=calendar,
            db_path=DB_PATH,
            artifact_dir=ARTIFACT_DIR,
            now=now,
        )

    return SessionRunner(
        calendar=calendar,
        schedule_store=store,
        watch=watch,
        scan_fn=scan_fn or scheduled,
        universe_db=DB_PATH,
        watch_enabled=enable_watch,
    )


def _build_watch():
    from src.execution.alpaca_adapter import AlpacaAdapter
    from src.risk.manager import RiskManager
    from src.watch.engine import WatchEngine
    from src.watch.feed import IexFeed
    from src.watch.orders import OrderStore

    orders = OrderStore(RUNTIME_DB)
    adapter = AlpacaAdapter()
    feed = IexFeed()
    return WatchEngine(orders, adapter, RiskManager(cash=100000), feed)


def start():
    """Run the New York session clock. Does not scan before the first due slot.

    The IEX reader is market data only. Buys stay disabled because
    ``ENTRY_RULE_DEFINED`` is false, and a missing trading client cannot
    invent an order.
    """
    import pandas as pd
    from apscheduler.schedulers.blocking import BlockingScheduler

    from src.control.instance import AlreadyRunning, InstanceLock
    from src.watch.iex_socket import start_iex_stream

    lock = InstanceLock()
    try:
        lock.acquire()
    except AlreadyRunning:
        print("Trading Bot is already running")
        return
    stop_feed = None
    try:
        runner = build_runner()
        stop_feed = start_iex_stream(
            runner.watch.feed,
            os.getenv("ALPACA_API_KEY", ""),
            os.getenv("ALPACA_API_SECRET", ""),
        )
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
    finally:
        if stop_feed is not None:
            stop_feed()
        lock.release()


if __name__ == "__main__":
    start()

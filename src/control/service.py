"""One control process for the initial scan, the session clock, and the watch.

The scan body and the watch minute stay in the existing Universe and watch
modules. This process only decides which of those entry points run, keeps
two scans from overlapping, and publishes status.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import pandas as pd

from src.control.instance import AlreadyRunning, InstanceLock
from src.control.permissions import NO_BUY, LaunchOptions, trading_state
from src.universe.pipeline import UniverseScanStopped

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
_ERROR_LIMIT = 20


class ControlService:
    def __init__(
        self,
        options: LaunchOptions,
        *,
        status_path: str | None = None,
        initial_scan=None,
        runner_factory=None,
        clock=None,
        scheduler_factory=None,
        iex_starter=None,
        on_event=None,
        on_progress=None,
        instance_lock: InstanceLock | None = None,
    ):
        self.options = options
        self.status_path = status_path or _default_status_path()
        self.initial_scan = initial_scan or self._production_initial_scan
        self.runner_factory = runner_factory or self._default_runner
        self.clock = clock or (lambda: pd.Timestamp.now(tz=NEW_YORK))
        self.scheduler_factory = scheduler_factory or _background_scheduler
        self.iex_starter = iex_starter if iex_starter is not None else _default_iex_starter
        self.on_event = on_event
        self.on_progress = on_progress
        self.instance = instance_lock or InstanceLock()
        self.runner = None
        self.loops_active = False
        self.last_scan_at = ""
        self.last_scan_result = ""
        self.last_scan_error = ""
        self.recent_errors: list[str] = []
        self.feed_health = "Unavailable"
        self.universe_running = False
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler = None
        self._stop_feed = None
        self._started = False
        self._released = False
        self._state_lock = threading.Lock()

    def start(self) -> None:
        """Run the initial scan, then arm the services that were requested."""
        if self._started:
            raise AlreadyRunning("Trading Bot is already running")
        self.instance.acquire()
        self._started = True
        self._released = False
        self._stop.clear()
        self._emit(f"Buy permission: {self.options.buy_permission}. Trading execution: {trading_state(self.options.buy_permission).trading_execution}.")
        self._publish()
        try:
            self._run_initial()
            if self._stop.is_set():
                return
            if self.options.services_requested:
                self._arm_services()
                self.dispatch_tick()
        except Exception:
            self.stop()
            raise

    def wait(self) -> None:
        self._stop.wait()

    def request_stop(self) -> None:
        """Ask the worker to exit without releasing the process lock yet."""
        self.stop(release=False)

    def stop(self, *, release: bool = True) -> None:
        """Stop the clock and the feed. The lock stays until the worker finishes."""
        self._stop.set()
        with self._state_lock:
            scheduler = self._scheduler
            self._scheduler = None
            stop_feed = self._stop_feed
            self._stop_feed = None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("scheduler shutdown failed")
                self._remember("scheduler shutdown failed")
        if stop_feed is not None:
            try:
                stop_feed()
            except Exception:
                logger.exception("market data feed stop failed")
                self._remember("market data feed stop failed")
        if stop_feed is not None:
            self.feed_health = "Unavailable"
        self.loops_active = False
        if not release:
            self._publish()
            return
        self.universe_running = False
        self.feed_health = "Unavailable"
        with self._state_lock:
            if self._released:
                return
            self._released = True
        self.instance.release()
        self._started = False
        self._publish()
        self._emit("Trading Bot stopped")

    def dispatch_tick(self) -> None:
        """Run one clock minute after any scan already in progress has finished.

        The clock is read after the scan lock is acquired, so a slot that
        became due during the initial scan is classified only when that scan
        is done. A slot outside its grace window is not replayed.
        """
        if self._stop.is_set():
            return
        with self._scan_lock:
            if self._stop.is_set():
                return
            self._dispatch(self.clock())

    def _run_initial(self) -> None:
        self._emit("Universe scan started")
        self.universe_running = True
        self._publish()
        try:
            with self._scan_lock:
                try:
                    self.initial_scan()
                except UniverseScanStopped:
                    self._record_scan("stopped")
                    self._emit("Universe scan stopped")
                    return
                except Exception as exc:
                    self._record_scan("failed", error=str(exc))
                    self._emit(f"Universe scan failed: {exc}")
                    logger.exception("initial universe scan failed")
                    return
                self._record_scan("success")
                self._emit("Universe scan succeeded")
        finally:
            self.universe_running = False
            self._publish()

    def _arm_services(self) -> None:
        self.runner = self.runner_factory(self.options)
        if self.options.enable_watch_engine:
            self._stop_feed = self.iex_starter(self.runner)
            self.feed_health = _feed_health(self.runner, self.clock()) if self._stop_feed is not None else "Unavailable"
            self._emit("Watch Engine scheduled")
        if self.options.enable_scheduler:
            self._emit("Scheduler started")
        scheduler = self.scheduler_factory()
        scheduler.add_job(
            self._cron,
            "cron",
            day_of_week="mon-fri",
            hour="10-16",
            minute="*",
            timezone=NEW_YORK,
            misfire_grace_time=30,
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        self._scheduler = scheduler
        self.loops_active = True
        self._publish()

    def _cron(self) -> None:
        try:
            self.dispatch_tick()
        except Exception as exc:
            logger.exception("session tick failed")
            self._remember(str(exc))
            self._publish()

    def _dispatch(self, now) -> None:
        if self.runner is None:
            return
        if self.options.enable_scheduler:
            self.runner.tick(now)
        elif self.options.enable_watch_engine:
            self.runner.tick_watch(now)
        self.feed_health = _feed_health(self.runner, now) if self.options.enable_watch_engine else "Unavailable"
        self._publish()

    def _default_runner(self, options: LaunchOptions):
        from scripts.scheduler import ARTIFACT_DIR, DB_PATH, build_runner, resolve_symbols
        from src.universe.scheduled_scan import run_scheduled_universe

        def scan(slot, now):
            self._emit("scheduled universe scan")
            self._report_progress(0)
            self.universe_running = True
            self._publish()
            try:
                result = run_scheduled_universe(
                    resolve_symbols(),
                    slot,
                    db_path=DB_PATH,
                    artifact_dir=ARTIFACT_DIR,
                    now=now,
                    stop_check=self._stop.is_set,
                    progress=self._report_progress,
                )
            except UniverseScanStopped:
                self._record_scan("stopped", slot_id=slot.slot_id)
                self._emit("Universe scan stopped")
                return None
            except Exception as exc:
                self._record_scan("failed", error=str(exc), slot_id=slot.slot_id)
                raise
            finally:
                self.universe_running = False
                self._publish()
            ok = bool(result is not None and getattr(result, "ok", False))
            self._record_scan("success" if ok else "failed", slot_id=slot.slot_id)
            self._emit("Universe scan succeeded" if ok else "Universe scan failed")
            return result

        return build_runner(enable_watch=options.enable_watch_engine, scan_fn=scan)

    def _record_scan(self, result: str, error: str = "", slot_id: str = "") -> None:
        moment = self.clock()
        self.last_scan_at = f"{moment.tz_convert(NEW_YORK).isoformat()}"
        if slot_id:
            self.last_scan_at = f"{self.last_scan_at} slot={slot_id}"
        self.last_scan_result = result
        self.last_scan_error = error
        if error:
            self._remember(error)
        self._publish()

    def _remember(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        self.recent_errors.append(text)
        del self.recent_errors[:-_ERROR_LIMIT]

    def _emit(self, message: str) -> None:
        logger.info("%s", message)
        if self.on_event is not None:
            self.on_event(message)

    def _publish(self) -> None:
        state = trading_state(self.options.buy_permission if self._started else NO_BUY)
        payload = {
            "pid": os.getpid() if self._started else None,
            "universe_running": self.universe_running,
            "scheduler_active": bool(self.loops_active and self.options.enable_scheduler and not self._stop.is_set()),
            "watch_engine_active": bool(self.loops_active and self.options.enable_watch_engine and not self._stop.is_set()),
            "buy_permission": state.buy_permission if self._started else NO_BUY,
            "trading_execution": state.trading_execution,
            "buys_active": state.buys_active,
            "last_scan_at": self.last_scan_at,
            "last_scan_result": self.last_scan_result,
            "feed_health": self.feed_health if self._started else "Unavailable",
            "recent_errors": list(self.recent_errors),
        }
        path = Path(self.status_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with self._state_lock:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(path)

    def _production_initial_scan(self):
        from scripts.scheduler import run_standalone_universe

        return run_standalone_universe(stop_check=self._stop.is_set, progress=self._report_progress)

    def _report_progress(self, percent: int) -> None:
        if self.on_progress is not None:
            self.on_progress(percent)


def _default_status_path() -> str:
    artifact_dir = os.getenv("ARTIFACT_DIR", "artifacts")
    return os.getenv("CONTROL_STATUS", os.path.join(artifact_dir, "control_status.json"))


def _background_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    return BackgroundScheduler(timezone=NEW_YORK)


def _default_iex_starter(runner):
    import os as _os

    from src.watch.iex_socket import start_iex_stream

    watch = getattr(runner, "watch", None)
    feed = getattr(watch, "feed", None)
    if feed is None:
        return None
    return start_iex_stream(feed, _os.getenv("ALPACA_API_KEY", ""), _os.getenv("ALPACA_API_SECRET", ""))


def _feed_health(runner, now) -> str:
    watch = getattr(runner, "watch", None)
    feed = getattr(watch, "feed", None)
    if feed is None or not hasattr(feed, "healthy"):
        return "Unavailable"
    try:
        return "healthy" if feed.healthy(now) else "unhealthy"
    except Exception:
        logger.exception("market data feed health could not be read")
        return "Unavailable"

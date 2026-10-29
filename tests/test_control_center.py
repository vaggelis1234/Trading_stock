"""Control center: permissions, one process, scan clock, help, and status.

These tests do not call Alpaca and do not submit orders.
"""

from datetime import date
import threading
import time
import tkinter as tk
import uuid

import pandas as pd
import pytest

from src.control.cli import build_parser, format_help, parse_args
from src.control.instance import AlreadyRunning, InstanceLock
from src.control.permissions import NO_BUY, YES_BUY, trading_state
from src.control.service import ControlService
from src.control.status import read_status
from src.desktop.app import TradingBotApp, buy_checkbox_state, launch_web_ui, universe_activity_line
from src.market.calendar import NyseCalendar
from src.persistence.store import save_universe_snapshots
from src.universe.pipeline import ScanProgress, UniverseScanStopped, run_universe
from src.universe.schedule_store import ScheduleStore
from src.universe.session_runner import SessionRunner
from src.watch.engine import ENTRY_RULE_DEFINED, WatchEngine
from src.watch.orders import OrderStore

DAY = date(2026, 10, 19)


class _Result:
    def __init__(self, ok, rows=None):
        self.ok = ok
        self.rows = rows or []


class _SpyAdapter:
    base_url = "https://paper-api.alpaca.markets"

    def __init__(self):
        self.submits = []
        self.client = object()

    def submit_limit_order(self, **kwargs):
        self.submits.append(kwargs)
        raise AssertionError("control tests must not submit orders")

    def get_order(self, order_id):
        raise AssertionError(order_id)


class _SpyWatch:
    def __init__(self):
        self.ticks = []
        self.feed = None

    def tick(self, now, session_close):
        self.ticks.append((now, session_close))

    def on_scan_success(self, slot, rows):
        return None

    def on_scan_failed(self, slot):
        return None

    def resume(self, rows):
        return None


class _Scheduler:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.jobs = []

    def add_job(self, func, *args, **kwargs):
        self.jobs.append(kwargs)

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.stopped = True


def _et(hour, minute):
    return pd.Timestamp(year=DAY.year, month=DAY.month, day=DAY.day, hour=hour, minute=minute, tz="America/New_York")


def _names():
    token = uuid.uuid4().hex
    return f"Local\\TradeStocksTest{token}"


def _service(tmp_path, options, **kwargs):
    status = tmp_path / "control_status.json"
    defaults = dict(
        status_path=str(status),
        initial_scan=lambda: None,
        scheduler_factory=_Scheduler,
        iex_starter=lambda runner: None,
        instance_lock=InstanceLock(_names()),
    )
    defaults.update(kwargs)
    return ControlService(options, **defaults)


def _runner(tmp_path, scan, watch=None, watch_enabled=False):
    return SessionRunner(
        calendar=NyseCalendar(),
        schedule_store=ScheduleStore(str(tmp_path / "schedule.db")),
        watch=watch,
        scan_fn=scan,
        universe_db=str(tmp_path / "universe.db"),
        watch_enabled=watch_enabled,
    )


def test_buy_permission_defaults_to_no_buy():
    options = parse_args([])
    assert options.buy_permission == NO_BUY
    assert options.enable_scheduler is False
    assert options.enable_watch_engine is False
    state = trading_state(options.buy_permission)
    assert state.buys_active is False
    assert state.trading_execution == "unavailable"
    assert ENTRY_RULE_DEFINED is False


def test_yes_buy_requires_scheduler():
    with pytest.raises(SystemExit):
        parse_args(["--yes_buy", "--enable_watch_engine"])


def test_yes_buy_requires_watch_engine():
    with pytest.raises(SystemExit):
        parse_args(["--yes_buy", "--enable_scheduler"])


def test_both_services_keep_buys_disabled():
    options = parse_args(["--enable_scheduler", "--enable_watch_engine", "--no_buy"])
    assert options.enable_scheduler is True
    assert options.enable_watch_engine is True
    assert options.buy_permission == NO_BUY
    explicit = parse_args(["--enable_scheduler", "--enable_watch_engine"])
    assert explicit.buy_permission == NO_BUY
    assert trading_state(YES_BUY).buys_active is False
    assert trading_state(YES_BUY).trading_execution == "unavailable"


def test_second_start_and_second_process_are_refused(tmp_path):
    name = _names()
    started = []
    first = _service(
        tmp_path,
        parse_args([]),
        initial_scan=lambda: started.append("first"),
        instance_lock=InstanceLock(name),
    )
    second = _service(
        tmp_path,
        parse_args([]),
        initial_scan=lambda: started.append("second"),
        instance_lock=InstanceLock(name),
    )
    first.start()
    try:
        with pytest.raises(AlreadyRunning):
            first.start()
        with pytest.raises(AlreadyRunning):
            second.start()
        assert started == ["first"]
    finally:
        first.stop()
    second.start()
    second.stop()
    assert started == ["first", "second"]


def test_initial_scan_leaves_the_due_slot_for_the_clock(tmp_path):
    times = [_et(10, 44)]
    order = []

    def clock():
        return times[0]

    def initial():
        order.append("initial")
        times[0] = _et(10, 46)

    def scheduled(slot, now):
        order.append(slot.slot_id)
        return _Result(True, [])

    watch = _SpyWatch()
    runner = _runner(tmp_path, scheduled, watch, watch_enabled=False)
    service = _service(
        tmp_path,
        parse_args(["--enable_scheduler"]),
        initial_scan=initial,
        clock=clock,
        runner_factory=lambda options: runner,
    )
    service.start()
    try:
        assert order == ["initial", f"{DAY.isoformat()}T10:45"]
        assert watch.ticks == []
        assert runner.store.status(f"{DAY.isoformat()}T10:45") == "executed"
        assert runner.store.status(f"{DAY.isoformat()}T10:15") == "missed"
    finally:
        service.stop()


def test_slot_after_the_grace_window_is_not_replayed(tmp_path):
    times = [_et(10, 44)]
    scanned = []

    def clock():
        return times[0]

    def initial():
        times[0] = _et(10, 51)

    runner = _runner(tmp_path, lambda slot, now: scanned.append(slot.slot_id))
    service = _service(
        tmp_path,
        parse_args(["--enable_scheduler"]),
        initial_scan=initial,
        clock=clock,
        runner_factory=lambda options: runner,
    )
    service.start()
    try:
        assert scanned == []
        assert runner.store.status(f"{DAY.isoformat()}T10:45") == "missed"
    finally:
        service.stop()


def test_scheduled_tick_waits_until_the_scan_lock_is_free(tmp_path):
    scanned = []
    runner = _runner(tmp_path, lambda slot, now: scanned.append(slot.slot_id) or _Result(True, []))
    service = _service(tmp_path, parse_args(["--enable_scheduler"]), clock=lambda: _et(10, 46), runner_factory=lambda options: runner)
    service.runner = runner
    service._scan_lock.acquire()
    finished = threading.Event()

    def tick():
        service.dispatch_tick()
        finished.set()

    thread = threading.Thread(target=tick)
    thread.start()
    time.sleep(0.2)
    assert scanned == []
    assert not finished.is_set()
    service._scan_lock.release()
    assert finished.wait(2)
    thread.join(1)
    assert scanned == [f"{DAY.isoformat()}T10:45"]


def test_help_lists_only_the_implemented_arguments():
    text = format_help()
    flags = set()
    for action in build_parser()._actions:
        flags.update(action.option_strings)
    assert flags == {"-h", "--help", "--enable_scheduler", "--enable_watch_engine", "--no_buy", "--yes_buy"}
    for flag in ("--enable_scheduler", "--enable_watch_engine", "--no_buy", "--yes_buy"):
        assert flag in text
    for banned in ("--log-level", "--symbols", "--size", "--force-scan"):
        assert banned not in text
    assert "Safe default" in text
    assert "not implemented" in text.lower() or "unavailable" in text.lower()


def test_status_does_not_start_services_or_write_files(tmp_path):
    universe = tmp_path / "universe.db"
    runtime = tmp_path / "runtime.db"
    status = tmp_path / "control_status.json"
    save_universe_snapshots(
        [
            {
                "scan_timestamp": "2026-10-19T14:15:00+00:00",
                "symbol": "AAPL",
                "hourly_close": 10.0,
                "daily_close": 10.0,
                "daily_ema200": 9.0,
                "rsi14": 50.0,
                "macd_hist": 0.1,
                "atr14": 0.2,
                "atr_percent": 0.02,
                "volume": 100.0,
                "volume_ratio": 1.0,
                "adv20": 10_000_000.0,
                "veto_status": "PASS",
                "veto_reasons": [],
                "earnings_check": "unavailable",
                "trend_score": 5.0,
                "momentum_score": 5.0,
                "volatility_score": 5.0,
                "volume_liquidity_score": 5.0,
                "final_score": 5.0,
                "rank": 1,
            }
        ],
        str(universe),
    )
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    report = read_status(
        status_path=str(status),
        universe_db=str(universe),
        runtime_db=str(runtime),
        now=_et(10, 0),
    )
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
    assert not runtime.exists()
    assert not status.exists()
    assert report.universe_running == "no"
    assert report.scheduler_active == "no"
    assert report.watch_engine_active == "no"
    assert report.last_scan_at == "2026-10-19T14:15:00+00:00"
    assert report.last_scan_result == "persisted"
    assert "10:15" in report.next_slot_new_york
    assert "17:15" in report.next_slot_athens
    assert report.feed_health == "Unavailable"
    assert report.buy_permission == NO_BUY
    assert report.trading_execution == "unavailable"
    assert report.buys_active == "no"
    assert report.recent_errors == ("Unknown",)


def test_live_yes_buy_still_reports_trading_unavailable(tmp_path):
    import json
    import os

    status = tmp_path / "control_status.json"
    status.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "universe_running": False,
                "scheduler_active": True,
                "watch_engine_active": True,
                "buy_permission": YES_BUY,
                "buys_active": True,
                "last_scan_at": "2026-10-19T14:15:00-04:00",
                "last_scan_result": "success",
                "feed_health": "unhealthy",
                "recent_errors": ["feed down"],
            }
        ),
        encoding="utf-8",
    )
    before = status.read_bytes()
    report = read_status(
        status_path=str(status),
        universe_db=str(tmp_path / "missing.db"),
        runtime_db=str(tmp_path / "missing-runtime.db"),
        now=_et(10, 0),
    )
    assert status.read_bytes() == before
    assert report.scheduler_active == "yes"
    assert report.watch_engine_active == "yes"
    assert report.buy_permission == YES_BUY
    assert report.trading_execution == "unavailable"
    assert report.buys_active == "no"
    assert report.recent_errors == ("feed down",)


def test_scan_failure_is_recorded_and_stop_releases_the_process(tmp_path):
    name = _names()

    def fail():
        raise RuntimeError("scan broke")

    scheduler = _Scheduler()
    stopped_feed = []
    runner = _runner(tmp_path, lambda slot, now: _Result(True, []))
    service = _service(
        tmp_path,
        parse_args(["--enable_scheduler", "--enable_watch_engine"]),
        initial_scan=fail,
        runner_factory=lambda options: runner,
        scheduler_factory=lambda: scheduler,
        iex_starter=lambda runner: (lambda: stopped_feed.append("stopped")),
        instance_lock=InstanceLock(name),
    )
    service.start()
    assert service.last_scan_result == "failed"
    assert "scan broke" in service.recent_errors
    assert scheduler.started is True
    service.stop()
    service.stop()
    assert scheduler.stopped is True
    assert stopped_feed == ["stopped"]
    follower = _service(tmp_path, parse_args([]), instance_lock=InstanceLock(name))
    follower.start()
    follower.stop()


def test_startup_error_stops_and_unlocks(tmp_path):
    name = _names()

    def broken_clock():
        raise RuntimeError("clock failed")

    service = _service(
        tmp_path,
        parse_args(["--enable_scheduler"]),
        scheduler_factory=broken_clock,
        runner_factory=lambda options: _runner(tmp_path, lambda slot, now: _Result(True, [])),
        instance_lock=InstanceLock(name),
    )
    with pytest.raises(RuntimeError, match="clock failed"):
        service.start()
    service.stop()
    follower = _service(tmp_path, parse_args([]), instance_lock=InstanceLock(name))
    follower.start()
    follower.stop()


def test_yes_buy_does_not_submit_orders(tmp_path):
    adapter = _SpyAdapter()
    engine = WatchEngine(OrderStore(str(tmp_path / "orders.db")), adapter)
    scanned = []

    def scheduled(slot, now):
        scanned.append(slot.slot_id)
        return _Result(True, [{"symbol": "AAPL", "rank": 1, "veto_status": "PASS", "final_score": 8.0}])

    runner = _runner(tmp_path, scheduled, engine, watch_enabled=True)
    options = parse_args(["--enable_scheduler", "--enable_watch_engine", "--yes_buy"])
    service = _service(
        tmp_path,
        options,
        clock=lambda: _et(11, 15),
        runner_factory=lambda options: runner,
        iex_starter=lambda runner: (lambda: None),
    )
    service.start()
    try:
        assert options.buy_permission == YES_BUY
        assert trading_state(options.buy_permission).buys_active is False
        assert engine.buy_gate.entry_rule_defined is False
        assert engine.order_sizer is None
        assert scanned == [f"{DAY.isoformat()}T11:15"]
        assert adapter.submits == []
        assert engine.positions.exit_submissions == 0
    finally:
        service.stop()


def test_watch_only_does_not_run_a_universe_slot(tmp_path):
    scanned = []
    watch = _SpyWatch()
    runner = _runner(tmp_path, lambda slot, now: scanned.append(slot.slot_id), watch, watch_enabled=True)
    service = _service(
        tmp_path,
        parse_args(["--enable_watch_engine"]),
        clock=lambda: _et(11, 0),
        runner_factory=lambda options: runner,
        iex_starter=lambda runner: (lambda: None),
    )
    service.start()
    try:
        assert scanned == []
        assert len(watch.ticks) == 1
        assert runner.store.known(DAY) == set()
    finally:
        service.stop()


def test_desktop_checkboxes_and_views_do_not_start_the_bot(tmp_path):
    assert buy_checkbox_state(False, True, True) == (False, "disabled")
    assert buy_checkbox_state(True, False, True) == (False, "disabled")
    assert buy_checkbox_state(True, True, False) == (False, "normal")
    assert buy_checkbox_state(True, True, True) == (True, "normal")

    root = tk.Tk()
    root.withdraw()
    created = []

    class _Idle:
        loops_active = False

        def start(self):
            return None

        def wait(self):
            return None

        def stop(self):
            self.stopped = True

    def factory(options, on_event=None, on_progress=None):
        service = _Idle()
        created.append(options)
        return service

    status_calls = []

    def reader():
        status_calls.append("read")
        return read_status(
            status_path=str(tmp_path / "missing-status.json"),
            universe_db=str(tmp_path / "missing.db"),
            runtime_db=str(tmp_path / "missing-runtime.db"),
            now=_et(10, 0),
        )

    app = TradingBotApp(root, service_factory=factory, status_reader=reader)
    try:
        assert str(app.buy_check.cget("state")) == "disabled"
        app.scheduler_var.set(True)
        app._sync_buy()
        assert app.buy_var.get() is False
        assert str(app.buy_check.cget("state")) == "disabled"
        app.watch_var.set(True)
        app.buy_var.set(True)
        app._sync_buy()
        assert str(app.buy_check.cget("state")) == "normal"
        assert app.buy_var.get() is True
        app.scheduler_var.set(False)
        app._sync_buy()
        assert app.buy_var.get() is False

        app.show_help()
        help_body = app.help_text.get("1.0", "end")
        assert help_body.strip() == format_help().strip()
        app.show_status()
        status_body = app.status_text.get("1.0", "end")
        assert status_calls == ["read"]
        assert "does not start or change services" in status_body
        assert "Trading execution: unavailable" in status_body
        assert app.service is None
        assert created == []

        app.scheduler_var.set(True)
        app.watch_var.set(True)
        app.buy_var.set(True)
        app._sync_buy()
        app.start_universe()
        app.worker.join(2)
        assert created[0].buy_permission == YES_BUY
        assert created[0].enable_scheduler is True
        assert created[0].enable_watch_engine is True
    finally:
        app.close()


def test_stop_check_aborts_universe_scan_before_market_data():
    with pytest.raises(UniverseScanStopped):
        run_universe(["AAPL"], stop_check=lambda: True)


def test_stop_during_scan_does_not_arm_the_scheduler(tmp_path):
    entered = threading.Event()
    armed = []

    def initial():
        entered.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            if service._stop.is_set():
                raise UniverseScanStopped("universe scan stopped")
            time.sleep(0.01)
        raise AssertionError("stop was not requested")

    service = _service(
        tmp_path,
        parse_args(["--enable_scheduler", "--enable_watch_engine"]),
        initial_scan=initial,
        scheduler_factory=lambda: armed.append(True),
    )
    worker = threading.Thread(target=service.start)
    worker.start()
    assert entered.wait(2)
    service.request_stop()
    worker.join(2)
    try:
        assert not worker.is_alive()
        assert service.last_scan_result == "stopped"
        assert armed == []
        assert service.loops_active is False
    finally:
        service.stop()


def test_pause_holds_the_scan_until_resume_or_stop(tmp_path):
    entered = threading.Event()
    finished = threading.Event()

    def initial():
        entered.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            if service._should_stop():
                raise UniverseScanStopped("universe scan stopped")
            time.sleep(0.01)
        finished.set()

    service = _service(tmp_path, parse_args([]), initial_scan=initial)
    worker = threading.Thread(target=service.start)
    worker.start()
    assert entered.wait(2)
    assert service.toggle_pause() is True
    time.sleep(0.3)
    assert not finished.is_set()
    assert worker.is_alive()
    assert service.toggle_pause() is False
    assert finished.wait(2)
    worker.join(2)
    service.stop()


def test_stop_button_stops_universe_and_leaves_the_window_open():
    root = tk.Tk()
    root.withdraw()
    ready = threading.Event()

    class _Running:
        loops_active = True

        def __init__(self):
            self.stopped = False

        def start(self):
            ready.set()
            while not self.stopped:
                time.sleep(0.01)
            self.loops_active = False

        def request_stop(self):
            self.stopped = True

        def wait(self):
            return None

        def stop(self):
            self.stopped = True

    def factory(options, on_event=None, on_progress=None):
        return _Running()

    app = TradingBotApp(root, service_factory=factory)
    try:
        assert str(app.stop_button.cget("state")) == "disabled"
        app.start_universe()
        assert ready.wait(2)
        assert str(app.stop_button.cget("state")) == "normal"
        assert str(app.start_button.cget("state")) == "disabled"
        app.stop_universe()
        app.worker.join(2)
        assert not app.worker.is_alive()
        assert app.root.winfo_exists()
    finally:
        app.close()


def test_scan_progress_reaches_100_only_when_the_scan_finishes():
    seen = []
    bar = ScanProgress(4, seen.append)
    bar.advance(2)
    assert seen[-1] == 50
    assert 100 not in seen
    bar.finish()
    assert seen[-1] == 100


def test_stopped_scan_reports_zero_and_does_not_finish(tmp_path):
    del tmp_path
    seen = []
    with pytest.raises(UniverseScanStopped):
        run_universe(["AAPL"], stop_check=lambda: True, progress=seen.append)
    assert seen == [0]


def test_action_button_fills_with_completed_work():
    from tkinter import ttk

    from src.desktop.calendar_panel import completion_percent

    root = tk.Tk()
    root.withdraw()
    app = TradingBotApp(root, service_factory=lambda *args, **kwargs: None)
    try:
        universe = app.activity.master.master
        assert universe.cget("text") == "Universe"
        assert not any(isinstance(child, ttk.Progressbar) for child in universe.winfo_children())
        assert completion_percent(1, 4) == 25
        app._arm_progress(app.start_button)
        app._set_progress(35)
        assert app.start_button.percent == 35
        assert app.calendar.delete_button.percent == 0
        app._set_progress(140)
        assert app.start_button.percent == 100
        app._clear_progress()
        assert app.start_button.percent == 0
    finally:
        app.close()


def test_activity_lines_name_only_the_enabled_services():
    assert universe_activity_line(True, True, repeat=False) == "start universe with scheduler and watch engine"
    assert universe_activity_line(True, False, repeat=True) == "new scan universe with scheduler"
    assert universe_activity_line(False, True, repeat=False) == "start universe with watch engine"
    assert universe_activity_line(False, False, repeat=False) == "start universe"


def test_dialog_logs_start_stop_and_the_next_scan():
    root = tk.Tk()
    root.withdraw()

    class _Idle:
        loops_active = False

        def start(self):
            return None

        def wait(self):
            return None

        def stop(self):
            return None

    app = TradingBotApp(root, service_factory=lambda options, on_event=None, on_progress=None: _Idle())
    try:
        app.scheduler_var.set(True)
        app.watch_var.set(True)
        app._sync_buy()
        app.start_universe()
        app.worker.join(2)
        app.scheduler_var.set(True)
        app.watch_var.set(False)
        app._sync_buy()
        app.start_universe()
        app.worker.join(2)
        text = app.activity.get("1.0", "end")
        assert "start universe with scheduler and watch engine" in text
        assert "new scan universe with scheduler" in text
        app._log("universe scan stopped")
        assert "universe scan stopped" in app.activity.get("1.0", "end")
    finally:
        app.close()


def test_web_ui_button_reuses_a_running_page(tmp_path):
    del tmp_path
    root = tk.Tk()
    root.withdraw()
    opened = []
    app = TradingBotApp(root, web_opener=lambda: opened.append("click") or "opened")
    try:
        app.open_web_ui()
        assert opened == ["click"]
        assert "web ui opened" in app.activity.get("1.0", "end")
    finally:
        app.close()


def test_launch_web_ui_starts_vite_and_api_only_when_they_are_down(tmp_path):
    opened = []
    commands = []

    def popen(command, **kwargs):
        commands.append((command, kwargs))

    result = launch_web_ui(
        root=tmp_path,
        port_open=lambda: False,
        api_port_open=lambda: False,
        popen=popen,
        open_browser=lambda url: None,
        python="python",
        vite_command=["vite", "--host", "127.0.0.1", "--port", "5173"],
    )
    assert result == "started"
    assert commands[0][0][:6] == ["python", "-m", "uvicorn", "src.ui.app:app", "--host", "127.0.0.1"]
    assert commands[0][0][-1] == "8000"
    assert commands[0][1]["cwd"] == str(tmp_path)
    assert commands[1][0][0] == "vite"
    assert commands[1][1]["cwd"] == str(tmp_path / "web")
    assert launch_web_ui(
        root=tmp_path,
        port_open=lambda: True,
        api_port_open=lambda: True,
        popen=popen,
        open_browser=lambda url: opened.append(url),
    ) == "opened"
    assert opened[-1].endswith(":5173")
    assert len(commands) == 2

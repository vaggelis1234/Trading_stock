"""Cash-session clock for Universe scans.

Times are America/New_York wall times. No test calls Alpaca or reads the
computer's local offset.
"""

from datetime import date, time
import threading

import pandas as pd
import pytest

from src.market.calendar import EARLY_CLOSE, NyseCalendar
from src.persistence.store import save_universe_snapshots
from src.universe.schedule import as_new_york, session_slots
from src.universe.schedule_store import ScheduleStore
from src.universe.session_runner import SessionRunner
from src.watch.engine import WatchEngine
from src.watch.orders import OrderStore


DAY = date(2026, 10, 19)
EARLY = date(2025, 11, 28)


class _Result:
    def __init__(self, ok, rows=None):
        self.ok = ok
        self.rows = rows or []


class _Adapter:
    base_url = "https://paper-api.alpaca.markets"

    def get_order(self, order_id):
        return {"id": order_id, "status": "accepted", "filled_qty": 0}

    def submit_limit_order(self, **kwargs):
        raise AssertionError("schedule tests must not submit orders")


def _et(day: date, hour: int, minute: int) -> pd.Timestamp:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, tz="America/New_York")


def _labels(slots):
    return [
        (
            slot.scan_at.strftime("%H:%M"),
            slot.window_start.strftime("%H:%M"),
            slot.window_end.strftime("%H:%M"),
            slot.kind,
            slot.window_minutes,
        )
        for slot in slots
    ]


def _runner(tmp_path, scan):
    calendar = NyseCalendar()
    watch = WatchEngine(OrderStore(str(tmp_path / "orders.db")), _Adapter())
    runner = SessionRunner(
        calendar=calendar,
        schedule_store=ScheduleStore(str(tmp_path / "schedule.db")),
        watch=watch,
        scan_fn=scan,
        universe_db=str(tmp_path / "universe.db"),
    )
    return runner, watch


def test_regular_session_slots_do_not_overlap_and_the_final_window_is_thirty_minutes():
    slots = session_slots(NyseCalendar().session(DAY))
    assert _labels(slots) == [
        ("10:45", "09:30", "10:30", "regular", 60),
        ("11:45", "10:30", "11:30", "regular", 60),
        ("12:45", "11:30", "12:30", "regular", 60),
        ("13:45", "12:30", "13:30", "regular", 60),
        ("14:45", "13:30", "14:30", "regular", 60),
        ("15:45", "14:30", "15:30", "regular", 60),
        ("16:15", "15:30", "16:00", "final", 30),
    ]
    for earlier, later in zip(slots, slots[1:]):
        assert earlier.window_end == later.window_start
        assert earlier.window_end <= later.window_start
    assert [half.strftime("%H:%M") for half in slots[-2].halves] == ["14:30", "15:00"]
    assert [half.strftime("%H:%M") for half in slots[-1].halves] == ["15:30"]
    assert all(slot.window_start.strftime("%H:%M") != "15:00" or slot.window_minutes != 60 for slot in slots)
    assert slots[-1].opens_buys is False
    assert slots[0].opens_buys is True


def test_early_close_keeps_only_completed_windows_and_the_real_last_half_hour():
    calendar = NyseCalendar()
    session = calendar.session(EARLY)
    assert session is not None
    assert session.close_et == EARLY_CLOSE
    slots = session_slots(session)
    assert _labels(slots) == [
        ("10:45", "09:30", "10:30", "regular", 60),
        ("11:45", "10:30", "11:30", "regular", 60),
        ("12:45", "11:30", "12:30", "regular", 60),
        ("13:15", "12:30", "13:00", "final", 30),
    ]
    assert all(slot.window_end <= _et(EARLY, 13, 0) for slot in slots)
    assert all(slot.scan_at.strftime("%H:%M") not in {"13:45", "14:45", "15:45", "16:15"} for slot in slots)


def test_holidays_and_weekends_have_no_scans():
    calendar = NyseCalendar()
    assert calendar.session(date(2026, 1, 1)) is None
    assert calendar.session(date(2026, 1, 19)) is None
    assert calendar.session(date(2026, 4, 3)) is None
    assert calendar.session(date(2026, 7, 3)) is None
    assert calendar.session(date(2026, 10, 17)) is None
    assert calendar.session(date(2026, 10, 18)) is None
    assert calendar.session(date(2025, 7, 3)).close_et == EARLY_CLOSE
    assert calendar.session(date(2026, 11, 27)).close_et == EARLY_CLOSE
    assert calendar.session(date(2026, 12, 24)).close_et == EARLY_CLOSE
    assert calendar.session(date(2026, 12, 25)) is None
    assert session_slots(None) == ()
    assert session_slots(calendar.session(date(2026, 10, 17))) == ()


def test_dst_changes_the_utc_instant_and_not_the_new_york_clock():
    calendar = NyseCalendar()
    march = session_slots(calendar.session(date(2026, 3, 9)))[0]
    november = session_slots(calendar.session(date(2026, 11, 2)))[0]
    before = session_slots(calendar.session(date(2026, 3, 6)))[0]
    assert march.scan_at.strftime("%H:%M") == "10:45"
    assert november.scan_at.strftime("%H:%M") == "10:45"
    assert march.scan_at.tz_convert("UTC").strftime("%H:%M") == "14:45"
    assert november.scan_at.tz_convert("UTC").strftime("%H:%M") == "15:45"
    assert before.scan_at.tz_convert("UTC").strftime("%H:%M") == "15:45"


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError):
        as_new_york(pd.Timestamp("2026-10-19 10:45"))


def test_startup_does_not_scan_and_a_slot_runs_once(tmp_path):
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.slot_id)
        return _Result(True, [])

    runner, watch = _runner(tmp_path, scan)
    assert calls == []
    assert watch.started is False
    runner.tick(_et(DAY, 10, 45))
    runner.tick(_et(DAY, 10, 46))
    assert calls == [runner.scans[0]]
    assert watch.started is True
    assert runner.store.status(calls[0]) == "executed"


def test_missed_slots_are_recorded_and_not_replayed(tmp_path):
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.scan_at.strftime("%H:%M"))
        return _Result(True, [])

    runner, _watch = _runner(tmp_path, scan)
    runner.tick(_et(DAY, 13, 0))
    assert calls == []
    assert runner.store.status(f"{DAY.isoformat()}T10:45") == "missed"
    assert runner.store.status(f"{DAY.isoformat()}T11:45") == "missed"
    assert runner.store.status(f"{DAY.isoformat()}T12:45") == "missed"
    runner.tick(_et(DAY, 13, 45))
    assert calls == ["13:45"]


def test_a_failed_slot_is_not_run_again(tmp_path):
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.slot_id)
        return _Result(False, [])

    runner, watch = _runner(tmp_path, scan)
    runner.tick(_et(DAY, 10, 45))
    runner.tick(_et(DAY, 10, 47))
    assert calls == [f"{DAY.isoformat()}T10:45"]
    assert runner.store.status(calls[0]) == "failed"
    assert watch.started is False


def test_holiday_and_weekend_ticks_do_not_scan(tmp_path):
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.slot_id)
        return _Result(True, [])

    runner, _watch = _runner(tmp_path, scan)
    runner.tick(_et(date(2026, 1, 1), 10, 45))
    runner.tick(_et(date(2026, 10, 17), 10, 45))
    assert calls == []


def test_concurrent_ticks_run_the_slot_once(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.slot_id)
        started.set()
        assert release.wait(2)

    runner, _watch = _runner(tmp_path, scan)
    first = threading.Thread(target=lambda: runner.tick(_et(DAY, 10, 45)))
    first.start()
    assert started.wait(2)
    second = threading.Thread(target=lambda: runner.tick(_et(DAY, 10, 46)))
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert calls == [f"{DAY.isoformat()}T10:45"]


def test_watch_resumes_from_the_saved_snapshot_without_a_new_scan(tmp_path):
    calls = []

    def scan(slot, now):
        del now
        calls.append(slot.slot_id)
        return _Result(True, [])

    db = str(tmp_path / "universe.db")
    save_universe_snapshots(
        [
            {
                "scan_timestamp": "2026-10-19T14:45:00+00:00",
                "symbol": "AAPL",
                "veto_status": "PASS",
                "veto_reasons": [],
                "earnings_check": "unavailable",
                "rank": 1,
                "final_score": 8,
            }
        ],
        db,
        snapshot_kind="CURRENT",
    )
    runner, watch = _runner(tmp_path, scan)
    runner.universe_db = db
    slot_id = f"{DAY.isoformat()}T10:45"
    from src.universe.schedule import session_slots

    slot = next(item for item in session_slots(NyseCalendar().session(DAY)) if item.slot_id == slot_id)
    assert runner.store.claim(slot)
    runner.store.finish(slot, "executed")
    runner.tick(_et(DAY, 12, 0))
    assert calls == []
    assert watch.started is True
    assert [row["symbol"] for row in watch.candidates] == ["AAPL"]


def test_startup_helper_is_unused_when_the_runner_is_constructed(tmp_path):
    runner, watch = _runner(tmp_path, lambda slot, now: (_ for _ in ()).throw(AssertionError("scan")))
    assert runner.scans == []
    assert watch.started is False

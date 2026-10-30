from datetime import date

import pandas as pd

from src.data.scan_scheduler import (
    SCAN_FINISHED,
    due_scan,
    format_scan_clock,
    next_scan_message,
    run_download_scheduler,
    scan_begins_message,
    scan_period,
    upcoming_scan,
)

FRIDAY = date(2026, 10, 30)


def _at(hour, minute, day=FRIDAY):
    return pd.Timestamp(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=hour,
        minute=minute,
        tz="America/New_York",
    )


def test_before_the_open_the_next_scan_names_the_first_window():
    now = _at(9, 0)
    slot = upcoming_scan(now)
    assert due_scan(now) is None
    assert format_scan_clock(slot, now) == "16:15:00"
    assert scan_period(slot) == "09:30:00-10:00:00"
    assert next_scan_message("16:15:00", "09:30:00-10:00:00") == (
        "επόμενο scan 16:15:00 για την περίοδο [09:30:00-10:00:00]"
    )
    assert scan_begins_message("09:30:00-10:00:00") == "σκαν ξεκινάει για την περίοδο [09:30:00-10:00:00]"


def test_a_scan_stays_due_through_its_grace_window():
    assert due_scan(_at(10, 15)).scan_at == _at(10, 15)
    assert due_scan(_at(10, 19)) is not None
    assert due_scan(_at(10, 20)) is None
    nxt = upcoming_scan(_at(10, 20))
    assert format_scan_clock(nxt, _at(10, 20)) == "16:45:00"
    assert scan_period(nxt) == "10:00:00-10:30:00"
    assert next_scan_message("16:45:00", "10:00:00-10:30:00") == (
        "επόμενο scan 16:45:00 για την περίοδο [10:00:00-10:30:00]"
    )


def test_after_the_last_scan_the_next_one_is_the_next_session():
    now = _at(16, 20)
    slot = upcoming_scan(now)
    assert format_scan_clock(slot, now) == "2026-11-02 17:15:00"
    assert scan_period(slot) == "09:30:00-10:00:00"


def test_an_early_close_keeps_the_last_window_that_fits():
    now = pd.Timestamp("2026-11-27 12:00", tz="America/New_York")
    slot = upcoming_scan(now)
    assert format_scan_clock(slot, now) == "19:15:00"
    assert scan_period(slot) == "11:30:00-12:00:00"


def test_the_clock_downloads_one_window_then_names_the_next_scan():
    state = {"now": _at(9, 0)}
    logs = []
    downloaded = []

    def clock():
        return state["now"]

    def download(slot):
        downloaded.append(scan_period(slot))
        return type("Result", (), {"stopped": False, "failed_symbols": ()})()

    def sleep(seconds):
        state["now"] = state["now"] + pd.Timedelta(seconds=seconds)

    def should_stop():
        return sum(line.startswith("επόμενο scan ") for line in logs) >= 2

    run_download_scheduler(
        clock=clock,
        download=download,
        log=logs.append,
        should_stop=should_stop,
        sleep=sleep,
    )
    assert downloaded == ["09:30:00-10:00:00"]
    assert logs == [
        next_scan_message("16:15:00", "09:30:00-10:00:00"),
        scan_begins_message("09:30:00-10:00:00"),
        SCAN_FINISHED,
        next_scan_message("16:45:00", "10:00:00-10:30:00"),
    ]


def test_a_due_scan_runs_once_and_is_not_replayed_inside_the_grace_window():
    state = {"now": _at(10, 16)}
    logs = []
    downloaded = []

    def download(slot):
        downloaded.append(slot.slot_id)
        return type("Result", (), {"stopped": False, "failed_symbols": ()})()

    def sleep(seconds):
        state["now"] = state["now"] + pd.Timedelta(seconds=seconds)

    run_download_scheduler(
        clock=lambda: state["now"],
        download=download,
        log=logs.append,
        should_stop=lambda: any(line.startswith("επόμενο scan ") for line in logs),
        sleep=sleep,
    )
    assert downloaded == ["2026-10-30T10:15"]
    assert logs[0] == scan_begins_message("09:30:00-10:00:00")
    assert logs[-1] == next_scan_message("16:45:00", "10:00:00-10:30:00")

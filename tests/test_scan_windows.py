"""Completed 30-minute windows used by a scheduled Universe scan."""

from datetime import date

import pandas as pd

from src.data.provider import _ALPACA_TIMEFRAMES, batch_size_for_window, prepare_ohlcv
from src.data.scan_windows import reconstruct_scheduled_bars
from src.data.sixty_minute_bars import INCOMPLETE_SIP_DELAY, MISSING_HALF, NAIVE_TIMESTAMP
from src.market.calendar import NyseCalendar
from src.universe.pipeline import run_universe
from src.universe.schedule import session_slots
from src.universe.scheduled_scan import run_scheduled_universe

DAY = date(2026, 10, 19)
EARLY = date(2025, 11, 28)
NY = "America/New_York"


def _et(day, label):
    hour, minute = label.split(":")
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=int(hour), minute=int(minute), tz=NY)


def _bars(day, specs):
    index = []
    rows = []
    for label, volume, close in specs:
        price = float(close)
        index.append(_et(day, label))
        rows.append((price, price + 1, price - 1, price, volume, 10))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume", "trade_count"], index=pd.DatetimeIndex(index))


def _by_label(result):
    return {bar.timestamp.tz_convert(NY).strftime("%H:%M"): bar for bar in result.bars}


def test_regular_windows_pair_halves_and_leave_the_final_half_hour_alone():
    slots = session_slots(NyseCalendar().session(DAY))
    specs = [
        ("09:00", 5, 1),
        ("09:30", 100, 10),
        ("10:00", 40, 11),
        ("10:30", 30, 12),
        ("11:00", 30, 12),
        ("11:30", 30, 12),
        ("12:00", 30, 12),
        ("12:30", 30, 12),
        ("13:00", 30, 12),
        ("13:30", 30, 12),
        ("14:00", 30, 12),
        ("14:30", 7, 20),
        ("15:00", 9, 21),
        ("15:30", 11, 22),
        ("16:00", 999, 30),
        ("16:30", 50, 31),
    ]
    result = reconstruct_scheduled_bars(_bars(DAY, specs), slots, _et(DAY, "16:15"))
    built = _by_label(result)
    assert list(built) == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert "15:00" not in built
    opening = built["09:30"]
    assert opening.minutes == 60
    assert opening.open == 10
    assert opening.high == 12
    assert opening.low == 9
    assert opening.close == 11
    assert opening.volume == 140
    assert opening.trade_count == 20
    later = built["14:30"]
    assert later.volume == 16
    assert later.volume != 16 + 11
    final = built["15:30"]
    assert final.minutes == 30
    assert final.volume == 11
    assert final.close == 22
    assert all(bar.volume not in {5, 999, 50} for bar in result.bars)


def test_missing_or_delayed_halves_are_not_fabricated():
    slots = session_slots(NyseCalendar().session(DAY))
    first = slots[0]
    delayed = reconstruct_scheduled_bars(
        _bars(DAY, [("09:30", 100, 10), ("10:00", 40, 11)]),
        [first],
        _et(DAY, "10:44"),
    )
    assert delayed.bars == ()
    assert any(item.reason == INCOMPLETE_SIP_DELAY for item in delayed.rejections)

    missing = reconstruct_scheduled_bars(_bars(DAY, [("09:30", 100, 10)]), [first], _et(DAY, "10:45"))
    assert missing.bars == ()
    assert any(item.reason == MISSING_HALF for item in missing.rejections)

    ready = reconstruct_scheduled_bars(
        _bars(DAY, [("09:30", 100, 10), ("10:00", 40, 11)]),
        [first],
        _et(DAY, "10:45"),
    )
    assert len(ready.bars) == 1
    assert ready.bars[0].volume == 140


def test_naive_timestamps_produce_no_bar():
    slots = session_slots(NyseCalendar().session(DAY))
    index = pd.DatetimeIndex([pd.Timestamp("2026-10-19 09:30")])
    frame = pd.DataFrame([(1, 1, 1, 1, 1)], columns=["open", "high", "low", "close", "volume"], index=index)
    result = reconstruct_scheduled_bars(frame, [slots[0]], _et(DAY, "10:45"))
    assert result.bars == ()
    assert result.rejections[0].reason == NAIVE_TIMESTAMP


def test_early_close_final_window_is_the_real_last_half_hour():
    slots = session_slots(NyseCalendar().session(EARLY))
    specs = [
        ("09:30", 10, 1),
        ("10:00", 10, 1),
        ("10:30", 10, 1),
        ("11:00", 10, 1),
        ("11:30", 10, 1),
        ("12:00", 10, 1),
        ("12:30", 4, 8),
        ("13:00", 80, 9),
    ]
    result = reconstruct_scheduled_bars(_bars(EARLY, specs), slots, _et(EARLY, "13:15"))
    built = _by_label(result)
    assert list(built) == ["09:30", "10:30", "11:30", "12:30"]
    assert built["12:30"].minutes == 30
    assert built["12:30"].volume == 4
    assert all(bar.timestamp.tz_convert(NY).strftime("%H:%M") != "12:00" for bar in result.bars)
    assert all(bar.volume != 80 for bar in result.bars)


def test_thirty_minute_requests_stay_on_the_historical_sip_timeframe():
    assert _ALPACA_TIMEFRAMES["30m"] == "30Min"
    assert batch_size_for_window("30m", "2026-10-19T14:00:00Z", "2026-10-19T16:00:00Z") == 50


def test_a_missing_slot_bar_does_not_fall_back_to_the_previous_hour(tmp_path):
    previous = _et(DAY, "14:30").tz_convert("UTC")
    required = _et(DAY, "15:30").tz_convert("UTC")
    frame = pd.DataFrame(
        [(20, 21, 19, 20, 5)],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([previous]),
    )
    empty = prepare_ohlcv(None)
    provider = _RejectingProvider()
    hidden = run_universe(
        ["AAPL"],
        provider=provider,
        db_path=str(tmp_path / "hidden.db"),
        bars_db=str(tmp_path / "hidden_bars.db"),
        artifact_dir=str(tmp_path / "hidden_artifacts"),
        hourly_frames={"AAPL": frame},
        daily_frames={"AAPL": empty},
        now=_et(DAY, "16:15"),
        lookback_bars=1,
    )
    assert hidden[0]["hourly_close"] == 20

    shown = run_universe(
        ["AAPL"],
        provider=provider,
        db_path=str(tmp_path / "shown.db"),
        bars_db=str(tmp_path / "shown_bars.db"),
        artifact_dir=str(tmp_path / "shown_artifacts"),
        hourly_frames={"AAPL": frame},
        daily_frames={"AAPL": empty},
        require_bar_at=required,
        now=_et(DAY, "16:15"),
        lookback_bars=1,
    )
    assert shown[0]["hourly_close"] is None
    assert shown[0]["veto_status"] == "BLOCKED_INSUFFICIENT_DATA"
    assert provider.calls == []


def test_scheduled_scan_uses_the_completed_pair_and_skips_a_missing_half(tmp_path):
    calendar = NyseCalendar()
    slot = session_slots(calendar.session(DAY))[0]
    provider = _RejectingProvider()
    complete = run_scheduled_universe(
        ["AAPL"],
        slot,
        calendar=calendar,
        provider=provider,
        bars_30m={"AAPL": _bars(DAY, [("09:00", 999, 1), ("09:30", 100, 10), ("10:00", 40, 11), ("16:00", 50, 9)])},
        daily_frames={"AAPL": prepare_ohlcv(None)},
        db_path=str(tmp_path / "universe.db"),
        bars_db=str(tmp_path / "bars.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        lookback_bars=1,
        hourly_lookback_days=1,
    )
    assert complete.ok is True
    assert complete.rows[0]["hourly_close"] == 11
    assert complete.rows[0]["volume"] == 140
    assert provider.calls == []

    missing = run_scheduled_universe(
        ["AAPL"],
        slot,
        calendar=calendar,
        provider=provider,
        bars_30m={"AAPL": _bars(DAY, [("09:30", 100, 10)])},
        daily_frames={"AAPL": prepare_ohlcv(None)},
        db_path=str(tmp_path / "missing.db"),
        bars_db=str(tmp_path / "missing_bars.db"),
        artifact_dir=str(tmp_path / "missing_artifacts"),
        lookback_bars=1,
        hourly_lookback_days=1,
    )
    assert missing.ok is False
    assert missing.rows == []
    assert {item["reason"] for item in missing.reasons} == {MISSING_HALF}


class _RejectingProvider:
    def __init__(self):
        self.calls = []

    def get_bars(self, *args, **kwargs):
        self.calls.append(("get_bars", args, kwargs))
        raise AssertionError("scheduled bars were already supplied")

    def get_bars_many(self, *args, **kwargs):
        self.calls.append(("get_bars_many", args, kwargs))
        raise AssertionError("scheduled bars were already supplied")

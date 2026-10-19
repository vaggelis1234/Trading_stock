"""Closed-bar reconstruction from the confirmed SIP probes.

Volumes and trade counts for 2026-10-14 and 2025-11-28 are the Historical SIP
figures already read for AAPL. Opens, highs, and lows of those probes were
not all recorded, so those prices are local fixture values. No test calls
Alpaca.
"""

from datetime import date

import pandas as pd
import pytest

from src.data.sixty_minute_bars import (
    CLOSING_MINUTE_DUPLICATE,
    CLOSING_MINUTE_INCOMPLETE,
    CLOSING_MINUTE_MISSING,
    DUPLICATE_TIMESTAMP,
    INCOMPLETE_SIP_DELAY,
    MISSING_HALF,
    NAIVE_TIMESTAMP,
    OFF_GRID,
    OUTSIDE_REGULAR_SESSION,
    STARTS_AT_SESSION_CLOSE,
    reconstruct_sixty_minute_bars,
)

REGULAR_DAY = date(2026, 10, 14)
EARLY_DAY = date(2025, 11, 28)

# ET label -> (volume, trade_count). Prices are filled by _bars.
REGULAR_PROBE = {
    "09:00": (227_379, 5_363),
    "09:30": (5_633_151, 105_505),
    "10:00": (3_395_393, 83_845),
    "10:30": (2_743_978, 85_978),
    "11:00": (2_365_177, 78_509),
    "11:30": (2_423_498, 75_822),
    "12:00": (3_043_416, 66_675),
    "12:30": (1_439_022, 37_146),
    "13:00": (2_272_297, 46_309),
    "13:30": (1_943_607, 45_669),
    "14:00": (1_692_834, 36_965),
    "14:30": (1_435_294, 34_828),
    "15:00": (1_982_004, 42_892),
    "15:30": (6_485_454, 124_671),
    "16:00": (12_381_543, 1_405),
    "16:30": (36_404, 650),
    "17:00": (13_308, 762),
}
REGULAR_CLOSING_MINUTE = (11_723_598, 269, 343.0)

EARLY_PROBE = {
    "09:00": (92_498, 1_992),
    "09:30": (3_286_146, 76_202),
    "10:00": (1_533_463, 54_216),
    "10:30": (1_138_867, 46_642),
    "11:00": (1_252_235, 50_185),
    "11:30": (1_149_333, 46_110),
    "12:00": (1_566_483, 32_252),
    "12:30": (3_597_164, 51_089),
    "13:00": (5_043_779, 1_634),
    "13:30": (59_426, 603),
    "16:00": (8_894, 167),
}
EARLY_CLOSING_MINUTE = (4_660_172, 415, 279.0)

REGULAR_WINDOWS = (
    "09:30",
    "10:30",
    "11:30",
    "12:30",
    "13:30",
    "14:30",
    "15:00",
)


def _et(day: date, label: str) -> pd.Timestamp:
    hour, minute = label.split(":")
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=int(hour), minute=int(minute), tz="America/New_York")


def _bars(day: date, probes: dict) -> pd.DataFrame:
    rows = []
    index = []
    for label, (volume, trades) in probes.items():
        price = float(int(label[:2]) + int(label[3:]) / 100)
        index.append(_et(day, label))
        rows.append((price, price + 1, price - 1, price + 0.5, volume, trades))
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume", "trade_count"], index=pd.DatetimeIndex(index))
    return frame


def _minute(day: date, label: str, volume: int, trades: int, close: float) -> pd.DataFrame:
    stamp = _et(day, label)
    return pd.DataFrame(
        [(close, close, close, close, volume, trades)],
        columns=["open", "high", "low", "close", "volume", "trade_count"],
        index=pd.DatetimeIndex([stamp]),
    )


def _reasons(result, reason: str):
    return [item for item in result.rejections if item.reason == reason]


def _by_et(result) -> dict:
    return {bar.timestamp.tz_convert("America/New_York").strftime("%H:%M"): bar for bar in result.bars}


def test_regular_session_windows_and_final_hour_exclude_the_close_bar():
    as_of = _et(REGULAR_DAY, "16:45")
    result = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, REGULAR_PROBE),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=as_of,
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    assert tuple(_by_et(result)) == REGULAR_WINDOWS
    final = _by_et(result)["15:00"]
    assert final.timestamp == _et(REGULAR_DAY, "15:00").tz_convert("UTC")
    assert final.open == pytest.approx(15.0)
    assert final.high == pytest.approx(16.3)
    assert final.low == pytest.approx(14.0)
    assert final.close == pytest.approx(15.8)
    assert final.volume == 1_982_004 + 6_485_454
    assert final.trade_count == 42_892 + 124_671
    assert final.volume != 12_381_543
    assert all(bar.volume != 12_381_543 for bar in result.bars)
    assert all(bar.timestamp.tz_convert("America/New_York").strftime("%H:%M") < "16:00" for bar in result.bars)
    opening = _by_et(result)["09:30"]
    assert opening.volume == 5_633_151 + 3_395_393
    assert opening.trade_count == 105_505 + 83_845
    later = _by_et(result)["14:30"]
    assert later.volume == 1_435_294 + 1_982_004
    assert {item.timestamp for item in _reasons(result, STARTS_AT_SESSION_CLOSE)} == {_et(REGULAR_DAY, "16:00").tz_convert("UTC")}
    assert _et(REGULAR_DAY, "09:00").tz_convert("UTC") in {item.timestamp for item in _reasons(result, OUTSIDE_REGULAR_SESSION)}
    assert _et(REGULAR_DAY, "16:30").tz_convert("UTC") in {item.timestamp for item in _reasons(result, OUTSIDE_REGULAR_SESSION)}


def test_closing_minute_is_the_one_minute_bar_not_the_thirty_minute_bar():
    result = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, REGULAR_PROBE),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:45"),
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    assert result.closing_minute is not None
    assert result.closing_minute.timestamp == pd.Timestamp("2026-10-14T20:00:00Z")
    assert result.closing_minute.volume == 11_723_598
    assert result.closing_minute.trade_count == 269
    assert result.closing_minute.close == 343.0
    assert result.closing_minute.volume != 12_381_543


def test_early_close_uses_the_real_close_and_its_own_minute():
    result = reconstruct_sixty_minute_bars(
        _bars(EARLY_DAY, EARLY_PROBE),
        session_day=EARLY_DAY,
        session_close="13:00",
        as_of=_et(EARLY_DAY, "13:45"),
        closing_minutes=_minute(EARLY_DAY, "13:00", *EARLY_CLOSING_MINUTE),
    )
    assert tuple(_by_et(result)) == ("09:30", "10:30", "11:30", "12:00")
    final = _by_et(result)["12:00"]
    assert final.volume == 1_566_483 + 3_597_164
    assert final.trade_count == 32_252 + 51_089
    assert all(bar.volume != 5_043_779 for bar in result.bars)
    assert result.closing_minute is not None
    assert result.closing_minute.timestamp == pd.Timestamp("2025-11-28T18:00:00Z")
    assert result.closing_minute.volume == 4_660_172
    assert result.closing_minute.trade_count == 415
    assert result.closing_minute.close == 279.0
    assert {item.timestamp for item in _reasons(result, STARTS_AT_SESSION_CLOSE)} == {_et(EARLY_DAY, "13:00").tz_convert("UTC")}


def test_edt_and_est_keep_the_same_new_york_window():
    probes = {"09:30": (10, 1), "10:00": (20, 2)}
    minutes = (1, 1, 1.0)
    for day, close, expected_utc in (
        (REGULAR_DAY, "16:00", "2026-10-14T13:30:00Z"),
        (EARLY_DAY, "13:00", "2025-11-28T14:30:00Z"),
    ):
        result = reconstruct_sixty_minute_bars(
            _bars(day, probes),
            session_day=day,
            session_close=close,
            as_of=_et(day, "16:45"),
            closing_minutes=_minute(day, close, *minutes) if close == "16:00" else _minute(day, "13:00", *minutes),
        )
        assert result.bars[0].timestamp == pd.Timestamp(expected_utc)
        assert result.bars[0].timestamp.tz_convert("America/New_York").strftime("%H:%M") == "09:30"


def test_missing_either_half_emits_no_sixty_minute_bar():
    probes = dict(REGULAR_PROBE)
    del probes["09:30"]
    missing_first = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, probes),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:45"),
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    assert "09:30" not in _by_et(missing_first)
    assert "10:30" in _by_et(missing_first)
    assert any(item.window_start == _et(REGULAR_DAY, "09:30").tz_convert("UTC") for item in _reasons(missing_first, MISSING_HALF))

    probes = dict(REGULAR_PROBE)
    del probes["10:00"]
    missing_second = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, probes),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:45"),
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    assert "09:30" not in _by_et(missing_second)
    assert _reasons(missing_second, MISSING_HALF)


def test_duplicate_timestamp_blocks_only_the_windows_that_use_it():
    frame = _bars(REGULAR_DAY, REGULAR_PROBE)
    extra = frame.loc[[_et(REGULAR_DAY, "15:00")]].copy()
    extra.iloc[0, extra.columns.get_loc("volume")] = 1
    duplicated = pd.concat([frame, extra])
    result = reconstruct_sixty_minute_bars(
        duplicated,
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:45"),
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    present = _by_et(result)
    assert "14:30" not in present
    assert "15:00" not in present
    assert "13:30" in present
    assert _reasons(result, DUPLICATE_TIMESTAMP)


def test_sip_delay_rejects_an_incomplete_second_half_and_accepts_the_boundary():
    probes = {"09:30": (10, 1), "10:00": (20, 2)}
    too_soon = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, probes),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "10:44"),
        closing_minutes=None,
    )
    assert too_soon.bars == ()
    assert _reasons(too_soon, INCOMPLETE_SIP_DELAY)
    assert _reasons(too_soon, CLOSING_MINUTE_MISSING)

    ready = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, probes),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "10:45"),
        closing_minutes=None,
    )
    assert tuple(_by_et(ready)) == ("09:30",)
    assert ready.bars[0].volume == 30
    assert ready.bars[0].trade_count == 3


def test_closing_minute_follows_the_same_fifteen_minute_rule():
    bars = _bars(REGULAR_DAY, {"15:00": (3, 1), "15:30": (4, 1)})
    minute = _minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE)
    early = reconstruct_sixty_minute_bars(
        bars,
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:15"),
        closing_minutes=minute,
    )
    assert _by_et(early)["15:00"].volume == 7
    assert early.closing_minute is None
    assert _reasons(early, CLOSING_MINUTE_INCOMPLETE)

    released = reconstruct_sixty_minute_bars(
        bars,
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:16"),
        closing_minutes=minute,
    )
    assert released.closing_minute is not None
    assert released.closing_minute.volume == 11_723_598


def test_duplicate_closing_minute_is_not_emitted():
    minute = _minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE)
    duplicated = pd.concat([minute, minute.assign(volume=1)])
    result = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, {"15:00": (3, 1), "15:30": (4, 1)}),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "16:45"),
        closing_minutes=duplicated,
    )
    assert result.closing_minute is None
    assert _reasons(result, CLOSING_MINUTE_DUPLICATE)
    assert _by_et(result)["15:00"].volume == 7


def test_off_grid_and_naive_timestamps_produce_no_bar():
    off = _bars(REGULAR_DAY, {"09:30": (10, 1), "10:00": (20, 2)})
    odd = off.iloc[[0]].copy()
    odd.index = pd.DatetimeIndex([_et(REGULAR_DAY, "10:15")])
    odd.iloc[0, odd.columns.get_loc("volume")] = 99
    result = reconstruct_sixty_minute_bars(
        pd.concat([off, odd]),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "11:00"),
        closing_minutes=None,
    )
    assert result.bars[0].volume == 30
    assert _reasons(result, OFF_GRID)

    naive = off.tz_localize(None)
    rejected = reconstruct_sixty_minute_bars(
        naive,
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "11:00"),
        closing_minutes=None,
    )
    assert rejected.bars == ()
    assert rejected.closing_minute is None
    assert _reasons(rejected, NAIVE_TIMESTAMP)


def test_no_incomplete_bar_is_present_in_the_output():
    result = reconstruct_sixty_minute_bars(
        _bars(REGULAR_DAY, REGULAR_PROBE),
        session_day=REGULAR_DAY,
        session_close="16:00",
        as_of=_et(REGULAR_DAY, "15:50"),
        closing_minutes=_minute(REGULAR_DAY, "16:00", *REGULAR_CLOSING_MINUTE),
    )
    labels = tuple(_by_et(result))
    assert labels == ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30")
    assert "15:00" not in labels
    assert result.closing_minute is None
    cutoff = _et(REGULAR_DAY, "15:50").tz_convert("UTC") - pd.Timedelta(minutes=15)
    for bar in result.bars:
        assert bar.timestamp + pd.Timedelta(minutes=60) <= cutoff

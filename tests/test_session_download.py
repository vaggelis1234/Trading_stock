from datetime import date, timedelta

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.provider import BarsBatch
from src.data.session_download import (
    SessionChoiceError,
    calendar_session_dates,
    delete_sessions,
    download_calendar_days,
    download_sessions,
    parse_sessions,
    session_month,
)
from src.market.calendar import NyseCalendar

TODAY = date(2026, 10, 16)


class _Provider:
    def __init__(self, failed=()):
        self.calls = []
        self.failed = tuple(failed)

    def get_bars_many(self, symbols, start, end, interval):
        self.calls.append({"symbols": list(symbols), "start": start, "end": end, "interval": interval})
        start_ts = pd.Timestamp(start)
        index = pd.DatetimeIndex([start_ts.tz_convert("UTC")])
        frame = pd.DataFrame(
            {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0]},
            index=index,
        )
        frames = {symbol: frame for symbol in symbols if symbol not in self.failed}
        return BarsBatch(frames, self.failed)


def test_parse_sessions_keeps_market_days_and_refuses_the_weekend():
    chosen = parse_sessions(["2026-10-16", "2026-10-14", "2026-10-16"], today=TODAY)
    assert chosen == [date(2026, 10, 14), date(2026, 10, 16)]
    with pytest.raises(SessionChoiceError) as exc:
        parse_sessions(["2026-10-10"], today=TODAY)
    assert exc.value.code == "not_a_session"
    with pytest.raises(SessionChoiceError) as future:
        parse_sessions(["2026-10-19"], today=TODAY)
    assert future.value.code == "future_session"


def test_download_requests_only_the_selected_sessions(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    gap = pd.Timestamp("2026-10-15 13:30", tz="UTC")
    store.upsert_30m("AAPL", _bar(gap, 4.0), pd.Timestamp("2026-10-15 20:00", tz="UTC"))
    provider = _Provider()
    result = download_sessions(
        [date(2026, 10, 14), date(2026, 10, 16)],
        provider=provider,
        store=store,
        symbols=["AAPL", "MSFT"],
        clock=pd.Timestamp("2026-10-16 20:00", tz="UTC"),
    )
    assert result.bars == 8
    thirty = [call for call in provider.calls if call["interval"] == "30m"]
    daily = [call for call in provider.calls if call["interval"] == "1d"]
    assert [
        pd.Timestamp(call["start"]).tz_convert("America/New_York").date() for call in thirty
    ] == [date(2026, 10, 14), date(2026, 10, 16)]
    assert [
        pd.Timestamp(call["start"]).tz_convert("America/New_York").date() for call in daily
    ] == [date(2026, 10, 14), date(2026, 10, 16)]
    loaded = store.load_window("AAPL", "30m", "2026-10-01", "2026-10-20")
    days = sorted({stamp.tz_convert("America/New_York").date() for stamp in loaded.index})
    assert days == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    daily_bars = store.load_window("AAPL", "1d", "2026-10-01", "2026-10-20")
    daily_days = sorted({stamp.tz_convert("America/New_York").date() for stamp in daily_bars.index})
    assert daily_days == [date(2026, 10, 14), date(2026, 10, 16)]


def test_a_full_session_is_not_requested_again(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    clock = pd.Timestamp("2026-10-16 21:00", tz="UTC")
    _store_closed_session(store, date(2026, 10, 14), clock)
    _store_closed_session(store, date(2026, 10, 16), clock, symbols=("AAPL",))
    provider = _Provider()
    result = download_sessions(
        [date(2026, 10, 14), date(2026, 10, 16)],
        provider=provider,
        store=store,
        symbols=["AAPL", "MSFT"],
        clock=clock,
    )
    assert result.skipped == 1
    assert result.bars == 4
    assert [
        pd.Timestamp(call["start"]).tz_convert("America/New_York").date() for call in provider.calls
    ] == [date(2026, 10, 16), date(2026, 10, 16)]


def test_the_open_session_is_still_requested(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    clock = pd.Timestamp("2026-10-16 15:00", tz="UTC")
    _store_closed_session(store, date(2026, 10, 16), clock)
    provider = _Provider()
    result = download_sessions(
        [date(2026, 10, 16)],
        provider=provider,
        store=store,
        symbols=["AAPL", "MSFT"],
        clock=clock,
    )
    assert result.skipped == 0
    assert provider.calls


def test_calendar_marks_a_stored_session_and_disables_the_future(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    store.upsert_30m(
        "AAPL",
        _bar(pd.Timestamp("2026-10-16 13:30", tz="UTC"), 10.0),
        pd.Timestamp("2026-10-16 20:00", tz="UTC"),
    )
    month = session_month(2026, 10, store, TODAY)
    by_date = {day["date"]: day for day in month["days"]}
    assert month["symbols"] == 1
    assert by_date["2026-10-16"]["stored_symbols"] == 1
    assert by_date["2026-10-16"]["selectable"] is True
    assert by_date["2026-10-17"]["market"] is False
    assert by_date["2026-10-19"]["selectable"] is False
    assert by_date["2026-10-16"]["close"] == "16:00"


def test_sixty_calendar_days_keep_sessions_and_drop_the_boundary_weekend_and_holiday():
    today = date(2026, 10, 16)
    dates = calendar_session_dates(today, 60)
    calendar = NyseCalendar()
    boundary = today - timedelta(days=60)
    assert dates[-1] == today
    assert boundary not in dates
    assert all(boundary < day <= today for day in dates)
    assert all(calendar.session(day) is not None for day in dates)
    assert date(2026, 10, 17) not in dates
    assert date(2026, 9, 7) not in dates
    assert calendar.session(date(2026, 9, 7)) is None


def test_selected_download_stops_before_the_next_day(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    provider = _Provider()
    result = download_sessions(
        [date(2026, 10, 14), date(2026, 10, 16)],
        provider=provider,
        store=store,
        symbols=["AAPL"],
        clock=pd.Timestamp("2026-10-16 20:00", tz="UTC"),
        should_stop=lambda: len(provider.calls) >= 2,
    )
    assert result.stopped is True
    assert [call["interval"] for call in provider.calls] == ["30m", "1d"]
    assert all(
        pd.Timestamp(call["start"]).tz_convert("America/New_York").date() == date(2026, 10, 14)
        for call in provider.calls
    )


def test_calendar_day_download_stops_before_the_next_session(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    provider = _Provider()
    dates = [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]

    result = download_calendar_days(
        dates,
        provider=provider,
        store=store,
        symbols=["AAPL"],
        clock=pd.Timestamp("2026-10-16 20:00", tz="UTC"),
        should_stop=lambda: len(provider.calls) >= 1,
    )

    assert result.stopped is True
    assert [call["interval"] for call in provider.calls] == ["30m", "1d"]
    assert all(
        pd.Timestamp(call["start"]).tz_convert("America/New_York").date() == date(2026, 10, 14)
        for call in provider.calls
    )


def test_delete_sessions_removes_only_the_chosen_new_york_days(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    clock = pd.Timestamp("2026-10-16 20:00", tz="UTC")
    store.upsert_30m("AAPL", _bar(pd.Timestamp("2026-10-14 13:30", tz="UTC"), 1.0), clock)
    store.upsert_30m("AAPL", _bar(pd.Timestamp("2026-10-15 13:30", tz="UTC"), 2.0), clock)
    store.upsert_30m("MSFT", _bar(pd.Timestamp("2026-10-16 13:30", tz="UTC"), 3.0), clock)
    store.upsert_1d("AAPL", _bar(pd.Timestamp("2026-10-14", tz="America/New_York"), 1.0), clock)
    store.upsert_1d("AAPL", _bar(pd.Timestamp("2026-10-15", tz="America/New_York"), 2.0), clock)
    store.upsert_1d("MSFT", _bar(pd.Timestamp("2026-10-16", tz="America/New_York"), 3.0), clock)
    conn = store._connect()
    conn.execute(
        """INSERT INTO market_bars
           (symbol, interval, bar_timestamp, open, high, low, close, volume)
           VALUES ('AAPL', '1h', '2026-10-16T14:30:00+00:00', 1, 1, 1, 1, 1)"""
    )
    conn.commit()
    conn.close()

    removed = delete_sessions([date(2026, 10, 16), date(2026, 10, 14)], store=store)

    assert removed == 4
    aapl = store.load_window("AAPL", "30m", "2026-10-01", "2026-10-20")
    assert [stamp.tz_convert("America/New_York").date() for stamp in aapl.index] == [date(2026, 10, 15)]
    aapl_daily = store.load_window("AAPL", "1d", "2026-10-01", "2026-10-20")
    assert [stamp.tz_convert("America/New_York").date() for stamp in aapl_daily.index] == [date(2026, 10, 15)]
    assert store.load_window("MSFT", "30m", "2026-10-01", "2026-10-20").empty
    assert store.load_window("MSFT", "1d", "2026-10-01", "2026-10-20").empty
    hourly = store.load_window("AAPL", "1h", "2026-10-01", "2026-10-20")
    assert len(hourly) == 1
    assert store.coverage("MSFT", "30m") is None
    assert store.coverage("AAPL", "30m")["latest_bar"].startswith("2026-10-15")


def _store_closed_session(store, day, clock, symbols=("AAPL", "MSFT")):
    start = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(hours=9, minutes=30)
    index = pd.DatetimeIndex([start + pd.Timedelta(minutes=30 * step) for step in range(13)])
    frame = pd.DataFrame(
        {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0},
        index=index,
    )
    daily = _bar(pd.Timestamp(day, tz="America/New_York"), 10.5)
    for symbol in symbols:
        store.upsert_30m(symbol, frame, clock)
        store.upsert_1d(symbol, daily, clock)


def _bar(stamp, close):
    return pd.DataFrame(
        {"open": [close], "high": [close], "low": [close], "close": [close], "volume": [5.0]},
        index=pd.DatetimeIndex([stamp]),
    )


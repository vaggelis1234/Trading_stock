import sqlite3

import pandas as pd
import pytest
import requests

from src.data.bar_store import BarStore
from src.data.provider import (
    ALPACA_429_MAX_RETRIES,
    ALPACA_SYMBOL_BATCH_CAP,
    BarsBatch,
    AlpacaMarketDataProvider,
    batch_size_for_window,
)
from src.universe.pipeline import run_universe


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self.payload


class _Session:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        query = dict(params or {})
        self.calls.append(query)
        return self.handler(query)


def _provider(monkeypatch, handler):
    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "key-secret")
    monkeypatch.setattr("src.data.provider._sleep", lambda _seconds: None)
    session = _Session(handler)
    return AlpacaMarketDataProvider(session=session, request_delay=0.0), session


def _bar(stamp, close, volume=100):
    return {"t": stamp, "o": close, "h": close + 1, "l": close - 1, "c": close, "v": volume}


def _ohlcv(index, close):
    close = pd.Series(close, index=index, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1_000.0},
        index=index,
    )


def _page(bars, token):
    return _Response(200, {"bars": bars, "next_page_token": token})


def test_batch_sizes_follow_the_page_budget():
    end = pd.Timestamp("2026-10-10")
    daily_start = (end - pd.Timedelta(days=520)).strftime("%Y-%m-%d")
    hourly_start = (end - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    tail_start = (end - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    end_text = end.strftime("%Y-%m-%d")
    assert batch_size_for_window("1d", daily_start, end_text) == 19
    assert batch_size_for_window("1h", hourly_start, end_text) == 6
    assert batch_size_for_window("1h", tail_start, end_text) == ALPACA_SYMBOL_BATCH_CAP
    assert ALPACA_SYMBOL_BATCH_CAP == 50


def test_multi_page_batch_keeps_a_symbol_split_across_pages(monkeypatch):
    def handler(query):
        assert query["symbols"] == "AAPL,MSFT"
        assert query["feed"] == "sip"
        assert query["adjustment"] == "all"
        assert query["limit"] == 10000
        if query.get("page_token") is None:
            return _page({"AAPL": [_bar("2024-01-02T14:00:00Z", 1), _bar("2024-01-02T15:00:00Z", 2)]}, "page-2")
        assert query["page_token"] == "page-2"
        return _page(
            {
                "AAPL": [_bar("2024-01-02T16:00:00Z", 3)],
                "MSFT": [_bar("2024-01-02T15:00:00Z", 8)],
            },
            None,
        )

    provider, session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["AAPL", "MSFT"], "2024-01-02T14:00:00Z", "2024-01-03T00:00:00Z", "1h")
    assert result.failed == ()
    assert list(result.frames["AAPL"]["close"]) == [1, 2, 3]
    assert list(result.frames["MSFT"]["close"]) == [8]
    assert len(session.calls) == 2


def test_local_contract_keeps_start_and_drops_the_inclusive_end(monkeypatch):
    def handler(_query):
        return _page(
            {
                "AAPL": [
                    _bar("2024-01-02T14:00:00Z", 1),
                    _bar("2024-01-02T15:00:00Z", 2),
                    _bar("2024-01-03T00:00:00Z", 3),
                ]
            },
            None,
        )

    provider, _session = _provider(monkeypatch, handler)
    frame = provider.get_bars_many(["AAPL"], "2024-01-02T14:00:00Z", "2024-01-03T00:00:00Z", "1h").frames["AAPL"]
    assert list(frame.index) == [pd.Timestamp("2024-01-02T14:00:00Z"), pd.Timestamp("2024-01-02T15:00:00Z")]
    assert list(frame["close"]) == [1, 2]


def test_symbol_missing_after_complete_pagination_is_an_empty_frame(monkeypatch):
    def handler(query):
        if query.get("page_token") is None:
            return _page({"AAPL": [_bar("2024-01-02T15:00:00Z", 1)]}, "page-2")
        return _page({"MSFT": [_bar("2024-01-02T15:00:00Z", 2)]}, None)

    provider, _session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["AAPL", "MSFT", "GOOG"], "2024-01-01", "2024-02-01", "1h")
    assert result.failed == ()
    assert result.frames["GOOG"].empty
    assert list(result.frames["AAPL"]["close"]) == [1]
    assert list(result.frames["MSFT"]["close"]) == [2]


def test_repeated_page_token_fails_the_whole_batch(monkeypatch):
    def handler(_query):
        return _page({"AAPL": [_bar("2024-01-02T15:00:00Z", 1)]}, "again")

    provider, session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["AAPL", "MSFT"], "2024-01-01", "2024-02-01", "1h")
    assert result.frames == {}
    assert result.failed == ("AAPL", "MSFT")
    assert len(session.calls) == 2


def test_http_400_on_a_later_page_does_not_split(monkeypatch):
    def handler(query):
        if query.get("page_token"):
            return _Response(400, {"message": "bad page"})
        return _page({"AAPL": [_bar("2024-01-02T15:00:00Z", 1)], "MSFT": [_bar("2024-01-02T15:00:00Z", 2)]}, "next")

    provider, session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["AAPL", "MSFT"], "2024-01-01", "2024-02-01", "1h")
    assert result.frames == {}
    assert set(result.failed) == {"AAPL", "MSFT"}
    assert len(session.calls) == 2


def test_http_400_stops_when_both_halves_fail(monkeypatch):
    def handler(_query):
        return _Response(400, {"message": "bad request"})

    provider, session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["A", "B", "C", "D"], "2024-01-01", "2024-01-08", "1d")
    assert result.frames == {}
    assert set(result.failed) == {"A", "B", "C", "D"}
    assert len(session.calls) == 3
    assert session.calls[0]["symbols"] == "A,B,C,D"
    assert {session.calls[1]["symbols"], session.calls[2]["symbols"]} == {"A,B", "C,D"}


def test_http_400_keeps_the_half_that_succeeds(monkeypatch):
    def handler(query):
        symbols = query["symbols"]
        if "," in symbols or symbols == "BAD":
            return _Response(400, {"message": "bad symbol"})
        return _page({symbols: [_bar("2024-01-02T05:00:00Z", 4)]}, None)

    provider, session = _provider(monkeypatch, handler)
    result = provider.get_bars_many(["GOOD", "BAD"], "2024-01-01", "2024-01-08", "1d")
    assert list(result.frames["GOOD"]["close"]) == [4]
    assert result.failed == ("BAD",)
    assert [call["symbols"] for call in session.calls] == ["GOOD,BAD", "GOOD", "BAD"]


def test_429_retries_then_429_exhaustion_and_500_do_not_grow(monkeypatch):
    state = {"mode": "retry"}

    def handler(_query):
        if state["mode"] == "retry":
            state["mode"] = "ok"
            return _Response(429, headers={"Retry-After": "1.5"})
        if state["mode"] == "ok":
            return _page({"AAPL": [_bar("2024-01-02T05:00:00Z", 6)]}, None)
        if state["mode"] == "limit":
            return _Response(429, headers={"Retry-After": "0.2"})
        return _Response(500, {"message": "down"})

    provider, session = _provider(monkeypatch, handler)
    slept = []
    monkeypatch.setattr("src.data.provider._sleep", slept.append)
    result = provider.get_bars_many(["AAPL"], "2024-01-01", "2024-02-01", "1d")
    assert list(result.frames["AAPL"]["close"]) == [6]
    assert slept == [1.5]
    assert len(session.calls) == 2

    state["mode"] = "limit"
    session.calls.clear()
    slept.clear()
    limited = provider.get_bars_many(["AAPL"], "2024-01-01", "2024-02-01", "1d")
    assert limited.frames == {}
    assert limited.failed == ("AAPL",)
    assert len(session.calls) == ALPACA_429_MAX_RETRIES + 1

    state["mode"] = "down"
    session.calls.clear()
    down = provider.get_bars_many(["AAPL"], "2024-01-01", "2024-02-01", "1d")
    assert down.failed == ("AAPL",)
    assert len(session.calls) == 1


def test_brk_b_is_requested_as_brk_dot_b_and_stored_under_the_local_symbol(monkeypatch):
    def handler(query):
        assert query["symbols"] == "BRK.B"
        return _page({"BRK.B": [_bar("2024-06-03T04:00:00Z", 400)]}, None)

    provider, _session = _provider(monkeypatch, handler)
    frame = provider.get_bars_many(["BRK-B"], "2024-06-01", "2024-06-04", "1d").frames["BRK-B"]
    assert frame["close"].iloc[0] == 400


def test_one_minute_get_bars_still_requests_one_symbol(monkeypatch):
    def handler(query):
        assert query["symbols"] == "AAPL"
        assert query["timeframe"] == "1Min"
        return _page({"AAPL": [_bar("2026-03-09T14:00:00Z", 10)]}, None)

    provider, session = _provider(monkeypatch, handler)
    frame = provider.get_bars("AAPL", "2026-03-09T14:00:00Z", "2026-03-09T15:00:00Z", "1m")
    assert len(session.calls) == 1
    assert frame["close"].iloc[0] == 10


def _counts(store, symbol, interval):
    conn = sqlite3.connect(store.path)
    try:
        rows = conn.execute(
            "SELECT bar_timestamp, close FROM market_bars WHERE symbol = ? AND interval = ? ORDER BY bar_timestamp",
            (symbol, interval),
        ).fetchall()
    finally:
        conn.close()
    return rows


def _seed(store, symbol, index, close, clock, interval="1d"):
    class One:
        def get_bars(self, _symbol, _start, _end, _interval):
            return _ohlcv(index, close)

    daily_start, hourly_start, end = _window(clock)
    start = daily_start if interval == "1d" else hourly_start
    store.sync(One(), symbol, interval, start, end, clock)


def _window(clock):
    end = pd.Timestamp(clock).tz_convert("UTC") + pd.Timedelta(days=1)
    return (
        (end - pd.Timedelta(days=520)).strftime("%Y-%m-%d"),
        (end - pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )


def test_paginated_batch_is_stored_per_symbol(monkeypatch, tmp_path):
    def handler(query):
        if query.get("page_token") is None:
            return _page({"AAPL": [_bar("2024-01-02T15:00:00Z", 1)]}, "page-2")
        return _page(
            {"AAPL": [_bar("2024-01-02T16:00:00Z", 2)], "MSFT": [_bar("2024-01-02T15:00:00Z", 9)]},
            None,
        )

    provider, _session = _provider(monkeypatch, handler)
    store = BarStore(str(tmp_path / "market_bars.db"))
    clock = pd.Timestamp("2024-01-03T18:00:00Z")
    _daily_start, _hourly_start, end = _window(clock)
    store.sync_many(provider, ["AAPL", "MSFT"], "1h", "2024-01-01", end, clock)
    assert [row[1] for row in _counts(store, "AAPL", "1h")] == [1.0, 2.0]
    assert [row[1] for row in _counts(store, "MSFT", "1h")] == [9.0]


def test_incomplete_batch_keeps_existing_bars_and_coverage(monkeypatch, tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    later = clock + pd.Timedelta(days=1)
    index = pd.bdate_range("2026-08-03", periods=5, tz="America/New_York")
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "AAPL", index, [10, 11, 12, 13, 14], clock)
    _seed(store, "MSFT", index, [20, 21, 22, 23, 24], clock)
    before = {symbol: _counts(store, symbol, "1d") for symbol in ("AAPL", "MSFT")}
    checked = {symbol: store.coverage(symbol, "1d")["checked_through"] for symbol in ("AAPL", "MSFT")}

    def handler(query):
        if query.get("page_token"):
            return _Response(500, {"message": "stopped"})
        return _page({"AAPL": [_bar("2026-09-15T04:00:00Z", 1)]}, "next")

    provider, _session = _provider(monkeypatch, handler)
    daily_start, _hourly_start, end = _window(later)
    store.sync_many(provider, ["AAPL", "MSFT"], "1d", daily_start, end, later)
    assert _counts(store, "AAPL", "1d") == before["AAPL"]
    assert _counts(store, "MSFT", "1d") == before["MSFT"]
    assert store.coverage("AAPL", "1d")["checked_through"] == checked["AAPL"]
    assert store.coverage("MSFT", "1d")["checked_through"] == checked["MSFT"]
    assert store.actions["failed"] == 2


def test_confirmed_absence_keeps_bars_and_advances_coverage(monkeypatch, tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    later = clock + pd.Timedelta(days=1)
    index = pd.bdate_range("2026-08-03", periods=4, tz="America/New_York")
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "GOOG", index, [1, 2, 3, 4], clock)
    before = _counts(store, "GOOG", "1d")

    def handler(query):
        if query.get("page_token") is None:
            return _page({"AAPL": [_bar("2026-09-15T04:00:00Z", 5)]}, "page-2")
        return _page({"MSFT": [_bar("2026-09-15T04:00:00Z", 6)]}, None)

    provider, _session = _provider(monkeypatch, handler)
    daily_start, _hourly_start, end = _window(later)
    store.sync_many(provider, ["AAPL", "MSFT", "GOOG"], "1d", daily_start, end, later)
    assert _counts(store, "GOOG", "1d") == before
    assert store.coverage("GOOG", "1d")["checked_through"].startswith("2026-09-17T20:30")
    assert len(_counts(store, "AAPL", "1d")) == 1
    assert len(_counts(store, "MSFT", "1d")) == 1


def test_empty_completed_response_does_not_delete_existing_bars(monkeypatch, tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    later = clock + pd.Timedelta(days=1)
    index = pd.bdate_range("2026-08-03", periods=4, tz="America/New_York")
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "AAPL", index, [1, 2, 3, 4], clock)
    before = _counts(store, "AAPL", "1d")

    provider, _session = _provider(monkeypatch, lambda _query: _page({}, None))
    daily_start, _hourly_start, end = _window(later)
    store.sync_many(provider, ["AAPL"], "1d", daily_start, end, later)
    assert _counts(store, "AAPL", "1d") == before
    assert store.coverage("AAPL", "1d")["checked_through"].startswith("2026-09-17T20:30")


def test_different_tail_windows_are_not_merged(tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    later = clock + pd.Timedelta(days=1)
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "EARLY", pd.bdate_range("2026-08-03", periods=5, tz="America/New_York"), [1, 2, 3, 4, 5], clock)
    _seed(store, "LATE", pd.bdate_range("2026-09-01", periods=5, tz="America/New_York"), [6, 7, 8, 9, 10], clock)
    calls = []

    class Many:
        def get_bars_many(self, symbols, start, end, interval):
            calls.append({"symbols": tuple(symbols), "start": start, "end": end, "interval": interval})
            return BarsBatch({symbol: pd.DataFrame() for symbol in symbols}, ())

    daily_start, _hourly_start, end = _window(later)
    store.sync_many(Many(), ["EARLY", "LATE"], "1d", daily_start, end, later)
    assert len(calls) == 2
    assert calls[0]["start"] != calls[1]["start"]
    assert {call["symbols"] for call in calls} == {("EARLY",), ("LATE",)}


def test_fresh_coverage_skips_the_request(tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    index = pd.date_range("2026-09-16T14:00:00Z", periods=4, freq="h")
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "AAPL", index, [1, 2, 3, 4], clock, interval="1h")

    class Many:
        def get_bars_many(self, *_args, **_kwargs):
            raise AssertionError("a fresh hourly series must not be requested")

    _daily_start, hourly_start, end = _window(clock)
    store.sync_many(Many(), ["AAPL"], "1h", hourly_start, end, clock)
    assert store.actions["skipped"] == 1


def test_empty_rebuild_refetch_does_not_delete_bars(tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    later = clock + pd.Timedelta(days=1)
    index = pd.bdate_range("2026-08-03", periods=6, tz="America/New_York")
    store = BarStore(str(tmp_path / "market_bars.db"))
    _seed(store, "AAPL", index, [10, 11, 12, 13, 14, 15], clock)
    before = _counts(store, "AAPL", "1d")
    checked = store.coverage("AAPL", "1d")["checked_through"]
    revised = _ohlcv(index, [99, 11, 12, 13, 14, 15])

    class Many:
        def get_bars_many(self, symbols, _start, _end, _interval):
            return BarsBatch({symbol: revised for symbol in symbols}, ())

        def get_bars(self, _symbol, _start, _end, _interval):
            return pd.DataFrame()

    daily_start, _hourly_start, end = _window(later)
    store.sync_many(Many(), ["AAPL"], "1d", daily_start, end, later)
    assert _counts(store, "AAPL", "1d") == before
    assert store.coverage("AAPL", "1d")["checked_through"] == checked
    assert store.actions["failed"] == 1


def test_persist_failure_keeps_the_sibling_that_committed(tmp_path):
    clock = pd.Timestamp("2026-09-16T20:30:00Z")
    index = pd.bdate_range("2026-08-03", periods=4, tz="America/New_York")
    frame = _ohlcv(index, [1, 2, 3, 4])
    store = BarStore(str(tmp_path / "market_bars.db"))
    original = store._persist

    def wrapped(symbol, interval, rows, required_start, now, replace_all):
        if symbol == "BAD":
            raise RuntimeError("disk full")
        return original(symbol, interval, rows, required_start, now, replace_all)

    store._persist = wrapped

    class Many:
        def get_bars_many(self, symbols, _start, _end, _interval):
            return BarsBatch({symbol: frame for symbol in symbols}, ())

    daily_start, _hourly_start, end = _window(clock)
    store.sync_many(Many(), ["GOOD", "BAD"], "1d", daily_start, end, clock)
    assert len(_counts(store, "GOOD", "1d")) == 4
    assert _counts(store, "BAD", "1d") == []
    assert store.coverage("BAD", "1d") is None
    assert store.actions["initialized"] == 1
    assert store.actions["failed"] == 1


def test_run_universe_uses_one_batch_per_timeframe(tmp_path):
    daily_idx = pd.bdate_range("2026-01-02", periods=5, tz="America/New_York")
    hourly_idx = pd.date_range("2026-09-16T14:00:00Z", periods=5, freq="h")
    daily = _ohlcv(daily_idx, [10, 11, 12, 13, 14])
    hourly = _ohlcv(hourly_idx, [1, 2, 3, 4, 5])
    calls = []

    class Many:
        def get_bars_many(self, symbols, start, end, interval):
            calls.append({"interval": interval, "symbols": tuple(symbols)})
            frame = daily if interval == "1d" else hourly
            return BarsBatch({symbol: frame for symbol in symbols}, ())

        def get_bars(self, *_args, **_kwargs):
            raise AssertionError("the 1-minute path is not part of this scan")

    rows = run_universe(
        ["AAPL", "MSFT"],
        provider=Many(),
        db_path=str(tmp_path / "universe.db"),
        bars_db=str(tmp_path / "market_bars.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        lookback_bars=1,
        now=pd.Timestamp("2026-09-16T18:00:00Z"),
    )
    assert [call["symbols"] for call in calls if call["interval"] == "1d"] == [("AAPL", "MSFT")]
    assert [call["symbols"] for call in calls if call["interval"] == "1h"] == [("AAPL", "MSFT")]
    assert {row["symbol"] for row in rows} == {"AAPL", "MSFT"}

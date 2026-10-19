import logging
import sqlite3

import pandas as pd
import pytest

from src.data.bar_store import HOURLY_OVERLAP, BarStore, completed_session_close
from src.data.provider import prepare_ohlcv
from src.persistence.store import SNAPSHOT_COLUMNS
from src.universe.metrics import compute_symbol_metrics
from src.universe.pipeline import _snapshot_timestamps, common_timestamps, evaluate_symbol, run_universe
from src.universe.ranking import rank_snapshots
from src.universe.scoring import load_universe_weights


class RecordingProvider:
    def __init__(self, frames=None, handler=None):
        self.frames = frames or {}
        self.handler = handler
        self.calls = []

    def get_bars(self, symbol, start, end, interval):
        self.calls.append({"symbol": symbol, "start": start, "end": end, "interval": interval})
        if self.handler is not None:
            return self.handler(symbol, start, end, interval)
        return self.frames[interval]


def _window(clock, daily_days=520, hourly_days=60):
    end = pd.Timestamp(clock)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    end = end.tz_convert("UTC") + pd.Timedelta(days=1)
    return (
        (end - pd.Timedelta(days=daily_days)).strftime("%Y-%m-%d"),
        (end - pd.Timedelta(days=hourly_days)).strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )


def _ohlcv(index, close, volume=1_000_000.0):
    close = pd.Series(close, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": volume,
        },
        index=index,
    )


def _daily_index(clock, periods=30):
    et = pd.Timestamp(clock).tz_convert("America/New_York").normalize()
    return pd.bdate_range(end=et - pd.Timedelta(days=1), periods=periods, tz="America/New_York")


def _calls(provider, interval):
    return [call for call in provider.calls if call["interval"] == interval]


def test_initialization_persists_the_full_window_and_reloads_it(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_start, hourly_start, end = _window(clock)
    daily = _ohlcv(_daily_index(clock), [100.0 + i for i in range(30)])
    hourly_idx = pd.date_range(clock - pd.Timedelta(hours=40), periods=40, freq="h", tz="UTC")
    hourly = _ohlcv(hourly_idx, [50.0 + (i % 3) for i in range(40)], volume=20_000.0)
    provider = RecordingProvider({"1d": daily, "1h": hourly})
    store = BarStore(str(tmp_path / "market_bars.db"))

    loaded_daily = store.sync_and_load(provider, "AAPL", "1d", daily_start, end, clock)
    loaded_hourly = store.sync_and_load(provider, "AAPL", "1h", hourly_start, end, clock)

    assert _calls(provider, "1d")[0]["start"] == daily_start
    assert _calls(provider, "1d")[0]["end"] == end
    assert _calls(provider, "1h")[0]["start"] == hourly_start
    coverage = store.coverage("AAPL", "1d")
    assert coverage["window_start"] == daily_start
    assert coverage["checked_through"].startswith("2026-09-16T20:30")
    assert coverage["latest_bar"] is not None
    pd.testing.assert_frame_equal(loaded_daily, prepare_ohlcv(daily))
    pd.testing.assert_frame_equal(loaded_hourly, prepare_ohlcv(hourly))
    conn = sqlite3.connect(store.path)
    count = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    conn.close()
    assert count == len(daily) + len(hourly)


def test_current_coverage_does_not_call_the_provider(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_start, hourly_start, end = _window(clock)
    provider = RecordingProvider(
        {
            "1d": _ohlcv(_daily_index(clock), [100.0 + i for i in range(30)]),
            "1h": _ohlcv(pd.date_range(clock - pd.Timedelta(hours=5), periods=6, freq="h", tz="UTC"), [10.0] * 6),
        }
    )
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1d", daily_start, end, clock)
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)
    provider.calls.clear()

    assert store.sync(provider, "AAPL", "1d", daily_start, end, clock) == "skipped"
    assert store.sync(provider, "AAPL", "1h", hourly_start, end, clock) == "skipped"
    assert provider.calls == []


def test_stale_coverage_requests_a_short_tail(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    later = clock + pd.Timedelta(hours=3)
    daily_start, hourly_start, end = _window(clock)
    _, _, later_end = _window(later)
    hourly_idx = pd.date_range(clock - pd.Timedelta(hours=10), periods=11, freq="h", tz="UTC")
    provider = RecordingProvider(
        {
            "1d": _ohlcv(_daily_index(clock), [80.0 + i for i in range(30)]),
            "1h": _ohlcv(hourly_idx, [20.0 + i * 0.1 for i in range(11)]),
        }
    )
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)
    provider.calls.clear()

    action = store.sync(provider, "AAPL", "1h", hourly_start, later_end, later)
    assert action == "refreshed"
    assert len(provider.calls) == 1
    requested = provider.calls[0]["start"]
    assert requested > hourly_start
    assert (pd.Timestamp(requested) - pd.Timestamp(hourly_start)).days > 30
    latest = pd.Timestamp(store.coverage("AAPL", "1h")["latest_bar"])
    assert requested <= (latest - HOURLY_OVERLAP).strftime("%Y-%m-%d")


def test_daily_session_refresh_skips_when_checked_and_does_not_retry_the_weekend(tmp_path):
    friday = pd.Timestamp("2026-09-18 21:00", tz="UTC")
    saturday = pd.Timestamp("2026-09-19 15:00", tz="UTC")
    sunday = saturday + pd.Timedelta(days=1)
    monday_close = pd.Timestamp("2026-09-21 21:00", tz="UTC")
    daily_start, _, end = _window(friday)
    provider = RecordingProvider({"1d": _ohlcv(_daily_index(friday, periods=20), [100.0 + i for i in range(20)])})
    store = BarStore(str(tmp_path / "market_bars.db"))
    assert store.sync(provider, "AAPL", "1d", daily_start, end, friday) == "initialized"
    provider.calls.clear()

    assert store.sync(provider, "AAPL", "1d", *_window(saturday)[0::2], saturday) == "skipped"
    assert store.sync(provider, "AAPL", "1d", *_window(sunday)[0::2], sunday) == "skipped"
    assert provider.calls == []

    monday_start, _, monday_end = _window(monday_close)
    assert store.sync(provider, "AAPL", "1d", monday_start, monday_end, monday_close) == "refreshed"
    assert provider.calls[0]["start"] > monday_start
    assert completed_session_close(saturday) < pd.Timestamp(store.coverage("AAPL", "1d")["checked_through"])


def test_failed_provider_keeps_bars_and_the_next_run_retries(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    later = clock + pd.Timedelta(days=1)
    daily_start, _, end = _window(clock)
    provider = RecordingProvider({"1d": _ohlcv(_daily_index(clock, periods=12), [10.0 + i for i in range(12)])})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1d", daily_start, end, clock)
    before = store.coverage("AAPL", "1d")["checked_through"]
    conn = sqlite3.connect(store.path)
    bars_before = conn.execute("SELECT bar_timestamp, close FROM market_bars ORDER BY bar_timestamp").fetchall()
    conn.close()

    def explode(*_args, **_kwargs):
        raise RuntimeError("provider down")

    provider.get_bars = explode
    assert store.sync(provider, "AAPL", "1d", *_window(later)[0::2], later) == "failed"
    assert store.coverage("AAPL", "1d")["checked_through"] == before
    conn = sqlite3.connect(store.path)
    bars_after = conn.execute("SELECT bar_timestamp, close FROM market_bars ORDER BY bar_timestamp").fetchall()
    conn.close()
    assert bars_after == bars_before

    provider.get_bars = RecordingProvider.get_bars.__get__(provider, RecordingProvider)
    provider.calls.clear()
    later_start, _, later_end = _window(later)
    assert store.sync(provider, "AAPL", "1d", later_start, later_end, later) == "refreshed"
    assert provider.calls


def test_empty_success_advances_coverage_without_changing_bars(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    later = clock + pd.Timedelta(hours=4)
    _, hourly_start, end = _window(clock)
    hourly = _ohlcv(pd.date_range(clock - pd.Timedelta(hours=3), periods=4, freq="h", tz="UTC"), [5, 6, 7, 8])
    provider = RecordingProvider({"1h": hourly})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)
    conn = sqlite3.connect(store.path)
    before = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    conn.close()

    provider.frames["1h"] = pd.DataFrame()
    assert store.sync(provider, "AAPL", "1h", hourly_start, _window(later)[2], later) == "refreshed"
    assert pd.Timestamp(store.coverage("AAPL", "1h")["checked_through"]) == later.floor("min")
    conn = sqlite3.connect(store.path)
    after = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    conn.close()
    assert after == before


def test_duplicate_timestamps_collapse_and_repeat_updates_the_row(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_start, _, end = _window(clock)
    index = _daily_index(clock, periods=5)
    duplicated = _ohlcv(index.append(pd.DatetimeIndex([index[-1]])), [1, 2, 3, 4, 5, 9])
    provider = RecordingProvider({"1d": duplicated})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1d", daily_start, end, clock)
    conn = sqlite3.connect(store.path)
    closes = [row[0] for row in conn.execute("SELECT close FROM market_bars ORDER BY bar_timestamp")]
    conn.close()
    assert closes == [1, 2, 3, 4, 9]

    revised = _ohlcv(index, [1, 2, 3, 4, 11])
    provider.frames["1d"] = revised
    later = clock + pd.Timedelta(days=1)
    store.sync(provider, "AAPL", "1d", *_window(later)[0::2], later)
    conn = sqlite3.connect(store.path)
    last = conn.execute("SELECT close FROM market_bars ORDER BY bar_timestamp DESC LIMIT 1").fetchone()[0]
    count = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    conn.close()
    assert last == 11
    assert count == 5


def test_changed_completed_bar_rebuilds_the_current_window(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    later = clock + pd.Timedelta(days=1)
    daily_start, _, end = _window(clock)
    index = _daily_index(later, periods=8)
    original = _ohlcv(index, [100.0 + i for i in range(8)])
    changed = original.copy()
    changed.iloc[0, changed.columns.get_loc("close")] = 50.0
    calls = {"n": 0}

    def handler(_symbol, start, _end, _interval):
        calls["n"] += 1
        if calls["n"] == 1:
            return original
        if start == daily_start:
            return changed
        return changed

    provider = RecordingProvider(handler=handler)
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1d", daily_start, end, clock)
    later_start, _, later_end = _window(later)
    assert store.sync(provider, "AAPL", "1d", later_start, later_end, later) == "rebuilt"
    starts = [call["start"] for call in provider.calls]
    assert starts[-1] == later_start
    assert any(start != later_start for start in starts[1:])
    conn = sqlite3.connect(store.path)
    first_close = conn.execute("SELECT close FROM market_bars ORDER BY bar_timestamp LIMIT 1").fetchone()[0]
    conn.close()
    assert first_close == 50.0


def test_newest_daily_bar_can_change_without_a_rebuild(tmp_path):
    clock = pd.Timestamp("2026-09-16 15:00", tz="UTC")
    later = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    index = pd.bdate_range(end=clock.tz_convert("America/New_York").normalize(), periods=6, tz="America/New_York")
    original = _ohlcv(index, [10.0 + i for i in range(6)])
    updated = original.copy()
    updated.iloc[-1, updated.columns.get_loc("close")] = 80.0
    provider = RecordingProvider({"1d": original})
    store = BarStore(str(tmp_path / "market_bars.db"))
    daily_start, _, end = _window(clock)
    store.sync(provider, "AAPL", "1d", daily_start, end, clock)
    provider.frames["1d"] = updated
    provider.calls.clear()
    later_start, _, later_end = _window(later)
    assert store.sync(provider, "AAPL", "1d", later_start, later_end, later) == "refreshed"
    assert len(provider.calls) == 1
    assert provider.calls[0]["start"] != later_start
    conn = sqlite3.connect(store.path)
    last = conn.execute("SELECT close FROM market_bars ORDER BY bar_timestamp DESC LIMIT 1").fetchone()[0]
    conn.close()
    assert last == 80.0


def test_newest_hourly_bars_update_without_a_rebuild_and_older_changes_rebuild(tmp_path):
    clock = pd.Timestamp("2026-09-16 18:00", tz="UTC")
    later = clock + pd.Timedelta(hours=3)
    _, hourly_start, end = _window(clock)
    index = pd.date_range(clock - pd.Timedelta(hours=8), periods=9, freq="h", tz="UTC")
    original = _ohlcv(index, [30.0 + i for i in range(9)])
    near = original.copy()
    near.iloc[-1, near.columns.get_loc("close")] = 99.0
    provider = RecordingProvider({"1h": original})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)
    provider.frames["1h"] = near
    provider.calls.clear()
    assert store.sync(provider, "AAPL", "1h", hourly_start, _window(later)[2], later) == "refreshed"
    assert len(provider.calls) == 1

    older = near.copy()
    older.iloc[0, older.columns.get_loc("close")] = 1.0
    provider.frames["1h"] = older
    provider.calls.clear()
    much_later = later + pd.Timedelta(hours=3)
    action = store.sync(provider, "AAPL", "1h", hourly_start, _window(much_later)[2], much_later)
    assert action == "rebuilt"
    assert provider.calls[-1]["start"] == hourly_start


def test_revision_log_records_only_the_first_material_difference(tmp_path, caplog):
    clock = pd.Timestamp("2026-09-16 18:00", tz="UTC")
    later = clock + pd.Timedelta(hours=6)
    _, hourly_start, end = _window(clock)
    index = pd.date_range(clock - pd.Timedelta(hours=8), periods=9, freq="h", tz="UTC")
    original = _ohlcv(index, [30.0 + i for i in range(9)])
    changed = original.copy()
    changed.iloc[0, changed.columns.get_loc("close")] = 1.0
    changed.iloc[1, changed.columns.get_loc("close")] = 2.0
    provider = RecordingProvider({"1h": original})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)
    provider.frames["1h"] = changed

    with caplog.at_level(logging.INFO, logger="src.data.bar_store"):
        action = store.sync(provider, "AAPL", "1h", hourly_start, _window(later)[2], later)

    assert action == "rebuilt"
    revisions = [record.getMessage() for record in caplog.records if record.getMessage().startswith("bar revision ")]
    assert revisions == [
        "bar revision AAPL 1h bar_utc=2026-09-16T10:00:00+00:00 "
        "scan_utc=2026-09-17T00:00:00+00:00 age_hours=14",
        "bar revision column=close stored=30 fetched=1 absolute=29 relative=0.966666666667",
    ]
    assert "2026-09-16T11:00:00+00:00" not in " ".join(revisions)


def test_revision_log_lists_every_differing_column_without_changing_detection(tmp_path, caplog):
    clock = pd.Timestamp("2026-09-16 18:00", tz="UTC")
    later = clock + pd.Timedelta(hours=6)
    _, hourly_start, end = _window(clock)
    index = pd.date_range(clock - pd.Timedelta(hours=8), periods=9, freq="h", tz="UTC")
    original = prepare_ohlcv(_ohlcv(index, [30.0 + i for i in range(9)]))
    provider = RecordingProvider({"1h": original})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1h", hourly_start, end, clock)

    with caplog.at_level(logging.INFO, logger="src.data.bar_store"):
        assert store._revision_detected("AAPL", "1h", original, later) is False
    assert [record.getMessage() for record in caplog.records if "bar revision " in record.getMessage()] == []

    changed = original.copy()
    changed.iloc[0, changed.columns.get_loc("open")] = 40.0
    changed.iloc[0, changed.columns.get_loc("high")] = 50.0
    changed.iloc[0, changed.columns.get_loc("low")] = 20.0
    changed.iloc[0, changed.columns.get_loc("close")] = 1.0
    changed.iloc[0, changed.columns.get_loc("volume")] = 1_500_000.0
    changed.iloc[1, changed.columns.get_loc("close")] = 2.0
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.data.bar_store"):
        assert store._revision_detected("AAPL", "1h", changed, later) is True

    revisions = [record.getMessage() for record in caplog.records if record.getMessage().startswith("bar revision ")]
    assert revisions == [
        "bar revision AAPL 1h bar_utc=2026-09-16T10:00:00+00:00 "
        "scan_utc=2026-09-17T00:00:00+00:00 age_hours=14",
        "bar revision column=open stored=30 fetched=40 absolute=10 relative=0.333333333333",
        "bar revision column=high stored=30.2 fetched=50 absolute=19.8 relative=0.655629139073",
        "bar revision column=low stored=29.8 fetched=20 absolute=9.8 relative=0.328859060403",
        "bar revision column=close stored=30 fetched=1 absolute=29 relative=0.966666666667",
        "bar revision column=volume stored=1000000 fetched=1500000 absolute=500000 relative=0.5 percent=50",
    ]
    assert "2026-09-16T11:00:00+00:00" not in " ".join(revisions)


def test_load_clips_bars_outside_the_required_window(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_start, _, end = _window(clock)
    inside = _daily_index(clock, periods=5)
    early = pd.Timestamp(daily_start, tz="UTC") - pd.Timedelta(days=10)
    late = pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=12)
    index = inside.tz_convert("UTC").append(pd.DatetimeIndex([early, late]))
    frame = _ohlcv(index, list(range(len(index))))
    provider = RecordingProvider({"1d": frame})
    store = BarStore(str(tmp_path / "market_bars.db"))
    loaded = store.sync_and_load(provider, "AAPL", "1d", daily_start, end, clock)
    conn = sqlite3.connect(store.path)
    stored = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    conn.close()
    assert stored == len(index)
    assert loaded.index.min() >= pd.Timestamp(daily_start, tz="UTC")
    assert loaded.index.max() < pd.Timestamp(end, tz="UTC")
    assert len(loaded) == len(inside)


def test_failed_write_does_not_advance_coverage(tmp_path, monkeypatch):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_start, _, end = _window(clock)
    provider = RecordingProvider({"1d": _ohlcv(_daily_index(clock, periods=4), [1, 2, 3, 4])})
    store = BarStore(str(tmp_path / "market_bars.db"))
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        original = conn.execute

        def execute(sql, params=()):
            if isinstance(sql, str) and "INTO market_coverage" in sql:
                raise sqlite3.OperationalError("disk full")
            return original(sql, params)

        conn.execute = execute
        return conn

    monkeypatch.setattr("src.data.bar_store.sqlite3.connect", connect)
    assert store.sync(provider, "AAPL", "1d", daily_start, end, clock) == "failed"
    conn = real_connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM market_coverage").fetchone()[0] == 0
    conn.close()


def test_backfill_requests_the_required_start_without_deleting_older_rows(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    short_start = "2026-08-01"
    _, _, end = _window(clock)
    full_start, _, _ = _window(clock)
    index = _daily_index(clock, periods=15)
    provider = RecordingProvider({"1d": _ohlcv(index, [40.0 + i for i in range(15)])})
    store = BarStore(str(tmp_path / "market_bars.db"))
    store.sync(provider, "AAPL", "1d", short_start, end, clock)
    conn = sqlite3.connect(store.path)
    conn.execute(
        """INSERT INTO market_bars (symbol, interval, bar_timestamp, open, high, low, close, volume)
           VALUES ('AAPL', '1d', '2020-01-02T00:00:00+00:00', 1, 1, 1, 1, 1)"""
    )
    conn.commit()
    conn.close()
    provider.calls.clear()
    assert store.sync(provider, "AAPL", "1d", full_start, end, clock) == "backfilled"
    assert provider.calls[0]["start"] == full_start
    conn = sqlite3.connect(store.path)
    kept = conn.execute(
        "SELECT close FROM market_bars WHERE bar_timestamp = '2020-01-02T00:00:00+00:00'"
    ).fetchone()
    conn.close()
    assert kept[0] == 1
    assert store.coverage("AAPL", "1d")["window_start"] == full_start


def test_metrics_match_the_full_provider_frame_across_session_and_staleness(tmp_path):
    history = pd.bdate_range("2024-06-03", periods=220, tz="America/New_York")
    scan_day = history[-1]
    as_of = (scan_day + pd.Timedelta(hours=15)).tz_convert("UTC").floor("min")
    clock = as_of
    daily_start, hourly_start, end = _window(clock)
    daily_close = [100.0 + (i % 11) * 0.15 for i in range(len(history) - 1)] + [1.0]
    daily = _ohlcv(history, daily_close, volume=2_000_000.0)
    hourly_idx = pd.date_range(as_of - pd.Timedelta(hours=40), periods=41, freq="h", tz="UTC")
    hourly_idx = hourly_idx.append(pd.DatetimeIndex([as_of + pd.Timedelta(hours=1)]))
    hourly_close = [120.0 + ((i % 4) - 1) * 0.3 for i in range(len(hourly_idx) - 1)] + [999.0]
    hourly = _ohlcv(hourly_idx, hourly_close, volume=15_000.0)
    provider = RecordingProvider({"1d": daily, "1h": hourly})
    store = BarStore(str(tmp_path / "market_bars.db"))
    loaded_daily = store.sync_and_load(provider, "AAPL", "1d", daily_start, end, clock)
    loaded_hourly = store.sync_and_load(provider, "AAPL", "1h", hourly_start, end, clock)
    direct_daily = prepare_ohlcv(daily)
    direct_hourly = prepare_ohlcv(hourly)
    direct_daily = direct_daily[(direct_daily.index >= pd.Timestamp(daily_start, tz="UTC")) & (direct_daily.index < pd.Timestamp(end, tz="UTC"))]
    direct_hourly = direct_hourly[(direct_hourly.index >= pd.Timestamp(hourly_start, tz="UTC")) & (direct_hourly.index < pd.Timestamp(end, tz="UTC"))]
    pd.testing.assert_frame_equal(loaded_daily, direct_daily)
    pd.testing.assert_frame_equal(loaded_hourly, direct_hourly)

    stale_as_of = as_of + pd.Timedelta(hours=4)
    for stamp in (as_of, as_of - pd.Timedelta(hours=20), stale_as_of):
        direct = compute_symbol_metrics(direct_daily, direct_hourly, stamp)
        stored = compute_symbol_metrics(loaded_daily, loaded_hourly, stamp)
        assert direct == stored
    boundary = compute_symbol_metrics(loaded_daily, loaded_hourly, as_of)
    assert boundary["daily_close"] != pytest.approx(1.0)
    assert boundary["hourly_close"] != pytest.approx(999.0)
    stale = compute_symbol_metrics(loaded_daily, loaded_hourly, stale_as_of)
    assert stale["hourly_close"] is None
    assert stale["rsi14"] is None


def test_universe_snapshots_match_direct_evaluation(tmp_path):
    clock = pd.Timestamp("2026-09-16 20:30", tz="UTC")
    daily_up = _ohlcv(_daily_index(clock, periods=220), [100.0 + i * 0.2 for i in range(220)])
    daily_down = _ohlcv(_daily_index(clock, periods=220), [160.0 - i * 0.2 for i in range(220)])
    hourly_idx = pd.date_range(clock - pd.Timedelta(hours=30), periods=30, freq="h", tz="UTC")
    hourly = _ohlcv(hourly_idx, [140.0 + (i % 2) for i in range(30)], volume=250_000.0)
    frames = {
        ("UP", "1d"): daily_up,
        ("UP", "1h"): hourly,
        ("DOWN", "1d"): daily_down,
        ("DOWN", "1h"): hourly,
    }

    class MapProvider:
        def __init__(self):
            self.calls = []

        def get_bars(self, symbol, start, end, interval):
            self.calls.append((symbol, start, end, interval))
            if interval == "1m":
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return frames[(symbol, interval)]

    provider = MapProvider()
    db = tmp_path / "universe.db"
    rows = run_universe(
        ["UP", "DOWN"],
        provider=provider,
        db_path=str(db),
        bars_db=str(tmp_path / "market_bars.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        lookback_bars=4,
        now=clock,
    )
    assert rows[0]["symbol"] == "UP"
    assert rows[1]["veto_status"] == "BLOCKED_TREND"

    daily_start, hourly_start, end = _window(clock)
    prepared = {}
    for symbol in ("UP", "DOWN"):
        prepared[(symbol, "1d")] = prepare_ohlcv(frames[(symbol, "1d")])
        prepared[(symbol, "1h")] = prepare_ohlcv(frames[(symbol, "1h")])
        prepared[(symbol, "1d")] = prepared[(symbol, "1d")][
            (prepared[(symbol, "1d")].index >= pd.Timestamp(daily_start, tz="UTC"))
            & (prepared[(symbol, "1d")].index < pd.Timestamp(end, tz="UTC"))
        ]
        prepared[(symbol, "1h")] = prepared[(symbol, "1h")][
            (prepared[(symbol, "1h")].index >= pd.Timestamp(hourly_start, tz="UTC"))
            & (prepared[(symbol, "1h")].index < pd.Timestamp(end, tz="UTC"))
        ]
    timestamps = _snapshot_timestamps(
        common_timestamps({symbol: prepared[(symbol, "1h")] for symbol in ("UP", "DOWN")}, 4),
        clock,
    )
    weights = load_universe_weights()
    expected = []
    for as_of in timestamps:
        ranked = rank_snapshots(
            [
                evaluate_symbol(symbol, prepared[(symbol, "1d")], prepared[(symbol, "1h")], as_of, None, weights)
                for symbol in ("UP", "DOWN")
            ]
        )
        expected.extend(ranked)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        "SELECT * FROM universe_snapshots ORDER BY scan_timestamp, symbol"
    ).fetchall()
    conn.close()
    expected = sorted(expected, key=lambda row: (row["scan_timestamp"], row["symbol"]))
    assert len(stored) == len(expected)
    import json

    for got, want in zip(stored, expected):
        for column in SNAPSHOT_COLUMNS:
            if column == "veto_reasons":
                assert json.loads(got[column]) == list(want[column])
            else:
                assert got[column] == want[column], (column, got[column], want[column])


def _hour_frame(stamp, close=10.0):
    index = pd.DatetimeIndex([pd.Timestamp(stamp)])
    return _ohlcv(index, [close], volume=100.0)


def test_old_market_bars_gains_session_columns_without_new_rows(tmp_path):
    path = tmp_path / "market_bars.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE market_bars (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            bar_timestamp TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, interval, bar_timestamp)
        )"""
    )
    conn.execute(
        """INSERT INTO market_bars
           (symbol, interval, bar_timestamp, open, high, low, close, volume)
           VALUES ('AAPL', '1h', '2026-03-06T09:00:00+00:00', 1, 1, 1, 1, 1)"""
    )
    conn.commit()
    conn.close()

    store = BarStore(str(path))
    conn = sqlite3.connect(store.path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(market_bars)")]
    coverage_columns = [row[1] for row in conn.execute("PRAGMA table_info(market_coverage)")]
    count = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    stored = conn.execute(
        """SELECT session_type, observed_minutes, expected_minutes, coverage_ratio
           FROM market_bars"""
    ).fetchone()
    conn.close()
    assert columns[-4:] == ["session_type", "observed_minutes", "expected_minutes", "coverage_ratio"]
    assert "coverage_ratio" not in coverage_columns
    assert count == 1
    assert stored == (None, None, None, None)


def test_session_metadata_updates_an_existing_hour_only(tmp_path):
    stamp = pd.Timestamp("2026-03-09 10:00", tz="America/New_York")
    weekend = pd.Timestamp("2026-03-07 10:00", tz="America/New_York")
    store = BarStore(str(tmp_path / "market_bars.db"))
    clock = pd.Timestamp("2026-03-09 20:00", tz="UTC")
    provider = RecordingProvider({"1h": _hour_frame(stamp, close=12.0)})
    store.sync(provider, "AAPL", "1h", "2026-03-01", "2026-03-10", clock)
    before = store.coverage("AAPL", "1h")

    assert store.update_hourly_session("AAPL", weekend, pd.DataFrame()) is False
    assert store.hourly_session("AAPL", weekend) is None
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0] == 1
    conn.close()

    pending = store.update_hourly_session("AAPL", stamp)
    assert pending is True
    stored = store.hourly_session("AAPL", stamp)
    assert stored.session_type == "REGULAR"
    assert stored.expected_minutes == 60
    assert stored.observed_minutes is None
    assert stored.coverage_ratio is None

    minutes = pd.DataFrame(
        {"close": [1.0, 1.0]},
        index=pd.DatetimeIndex([stamp, stamp + pd.Timedelta(minutes=15)]),
    )
    assert store.update_hourly_session("AAPL", stamp, minutes) is True
    counted = store.hourly_session("AAPL", stamp)
    assert counted.observed_minutes == 2
    assert counted.coverage_ratio == 2 / 60
    assert store.coverage("AAPL", "1h")["checked_through"] == before["checked_through"]
    assert store.coverage("AAPL", "1h")["window_start"] == before["window_start"]

    off_grid = pd.Timestamp("2026-03-09 09:30", tz="America/New_York")
    assert store.update_hourly_session("AAPL", off_grid) is False
    loaded = store.load_window("AAPL", "1h", "2026-03-01", "2026-03-10")
    assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
    assert loaded["close"].iloc[0] == 12.0


def test_ohlcv_refresh_keeps_session_metadata(tmp_path):
    stamp = pd.Timestamp("2026-03-06 04:00", tz="America/New_York")
    later = stamp + pd.Timedelta(hours=4)
    store = BarStore(str(tmp_path / "market_bars.db"))
    clock = pd.Timestamp("2026-03-06 21:00", tz="UTC")
    provider = RecordingProvider({"1h": _ohlcv(pd.DatetimeIndex([stamp, later]), [10.0, 11.0])})
    store.sync(provider, "AAPL", "1h", "2026-03-01", "2026-03-10", clock)
    minutes = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([stamp]))
    assert store.update_hourly_session("AAPL", stamp, minutes) is True

    provider.frames["1h"] = _ohlcv(pd.DatetimeIndex([stamp, later]), [10.5, 11.0])
    store.sync(provider, "AAPL", "1h", "2026-03-01", "2026-03-10", clock + pd.Timedelta(hours=3))
    kept = store.hourly_session("AAPL", stamp)
    assert kept.session_type == "PRE_MARKET"
    assert kept.observed_minutes == 1
    assert kept.coverage_ratio == 1 / 60
    reloaded = store.load_window("AAPL", "1h", "2026-03-01", "2026-03-10")
    assert reloaded.loc[stamp.tz_convert("UTC"), "close"] == 10.5

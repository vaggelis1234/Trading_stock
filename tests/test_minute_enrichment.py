import pandas as pd

from src.data.minute_enrichment import ENRICHMENT_FIELDS, enrich_candidates, enrich_hourly_bar


HOUR = pd.Timestamp("2026-10-02 14:00", tz="America/New_York")
SCAN = pd.Timestamp("2026-10-02 15:00", tz="America/New_York")


def _minutes(start, count, volume=1.0, price=None):
    index = pd.date_range(start, periods=count, freq="min")
    rows = []
    for step, stamp in enumerate(index):
        opened = 100.0 + step if price is None else price
        closed = opened + 0.5
        rows.append(
            {
                "open": opened,
                "high": closed,
                "low": opened,
                "close": closed,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows, index=index)


def _enrich(minutes, as_of=SCAN, hourly_open=100.0, hourly_close=110.0):
    return enrich_hourly_bar("AAPL", as_of, HOUR, hourly_open, hourly_close, minutes, as_of)


def test_empty_lookup_records_zero_coverage_without_prices():
    record = _enrich(_minutes(HOUR, 0))
    assert record["session_type"] == "REGULAR"
    assert record["expected_minutes"] == 60
    assert record["observed_minutes"] == 0
    assert record["coverage_ratio"] == 0.0
    assert record["first_price"] is None
    assert record["last_price"] is None
    assert record["intrahour_return"] is None
    assert record["close_position"] is None
    assert record["minute_volume"] == 0.0
    assert record["top5_volume_share"] is None
    assert record["last10_volume_share"] is None
    assert record["return_5m"] is None
    assert record["return_10m"] is None
    assert record["mfe"] is None
    assert record["mae"] is None
    assert record["minutes_with_signal"] == 0
    assert record["minutes_against_signal"] == 0
    assert record["hourly_signal"] == "UP"
    assert "final_score" not in record
    assert set(record) == set(ENRICHMENT_FIELDS)


def test_sparse_minutes_use_only_real_bars():
    stamps = [
        HOUR,
        HOUR + pd.Timedelta(minutes=10),
        HOUR + pd.Timedelta(minutes=54),
    ]
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 108.0],
            "high": [102.0, 101.0, 112.0],
            "low": [99.0, 98.0, 107.0],
            "close": [101.0, 99.0, 111.0],
            "volume": [10.0, 30.0, 50.0],
        },
        index=pd.DatetimeIndex(stamps),
    )
    record = _enrich(frame)
    assert record["observed_minutes"] == 3
    assert record["coverage_ratio"] == 3 / 60
    assert record["first_price"] == 100.0
    assert record["last_price"] == 111.0
    assert record["intrahour_return"] == 0.11
    assert record["intrahour_high"] == 112.0
    assert record["intrahour_low"] == 98.0
    assert record["close_position"] == (111.0 - 98.0) / (112.0 - 98.0)
    assert record["minutes_with_signal"] == 2
    assert record["minutes_against_signal"] == 1
    assert record["minute_volume"] == 90.0
    assert record["top5_volume_share"] == 1.0
    assert record["last10_volume_share"] == 50.0 / 90.0
    assert record["return_5m"] is None
    assert record["return_10m"] is None
    assert record["mfe"] == (112.0 - 100.0) / 100.0
    assert record["mae"] == (100.0 - 98.0) / 100.0


def test_full_hour_matches_every_minute_in_the_bar():
    record = _enrich(_minutes(HOUR, 60))
    assert record["observed_minutes"] == 60
    assert record["coverage_ratio"] == 1.0
    assert record["first_price"] == 100.0
    assert record["last_price"] == 159.5
    assert record["intrahour_return"] == 0.595
    assert record["intrahour_high"] == 159.5
    assert record["intrahour_low"] == 100.0
    assert record["close_position"] == 1.0
    assert record["minutes_with_signal"] == 60
    assert record["minutes_against_signal"] == 0
    assert record["minute_volume"] == 60.0
    assert record["top5_volume_share"] == 5 / 60
    assert record["last10_volume_share"] == 10 / 60
    assert record["return_5m"] == 5 / 154.5
    assert record["return_10m"] == 10 / 149.5
    assert record["mfe"] == 0.595
    assert record["mae"] == 0.0


def test_minutes_outside_the_scan_and_the_hour_do_not_count():
    as_of = HOUR + pd.Timedelta(minutes=30)
    inside = _minutes(HOUR, 30)
    poison = pd.DataFrame(
        {
            "open": [1.0, 500.0, 999.0],
            "high": [1.0, 500.0, 999.0],
            "low": [1.0, 500.0, 999.0],
            "close": [1.0, 500.0, 999.0],
            "volume": [10_000.0, 10_000.0, 10_000.0],
        },
        index=pd.DatetimeIndex(
            [
                HOUR - pd.Timedelta(minutes=1),
                HOUR + pd.Timedelta(minutes=40),
                HOUR + pd.Timedelta(hours=1),
            ]
        ),
    )
    leaked = pd.concat([poison, inside]).sort_index()
    clean = _enrich(inside, as_of=as_of)
    dirty = _enrich(leaked, as_of=as_of)
    assert dirty == clean
    assert clean["observed_minutes"] == 30
    assert clean["last_price"] == 129.5
    assert clean["last10_volume_share"] == 0.0
    assert clean["return_5m"] == 5 / 124.5
    assert clean["return_10m"] == 10 / 119.5
    assert clean["minute_volume"] == 30.0


class _Provider:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def get_bars(self, symbol, start, end, interval):
        self.calls.append({"symbol": symbol, "start": start, "end": end, "interval": interval})
        return self.frames[symbol]


def test_partial_current_hour_keeps_only_minutes_before_the_scan():
    hour = pd.Timestamp("2026-03-09T17:00:00Z")
    scan = pd.Timestamp("2026-03-09T17:02:00Z")
    minutes = _minutes(hour, 5)
    record = enrich_hourly_bar("AAPL", scan, hour, 100.0, 101.0, minutes, scan)
    assert record["scan_timestamp"] == "2026-03-09T17:02:00+00:00"
    assert record["hour_timestamp"] == "2026-03-09T17:00:00+00:00"
    assert record["observed_minutes"] == 2
    assert record["expected_minutes"] == 60
    assert record["last_price"] == 101.5
    assert record["minute_volume"] == 2.0


def test_previous_hour_stops_at_the_hour_boundary():
    hour = pd.Timestamp("2026-03-09T16:00:00Z")
    scan = pd.Timestamp("2026-03-09T17:02:00Z")
    minutes = _minutes(hour, 70)
    record = enrich_hourly_bar("AAPL", scan, hour, 100.0, 110.0, minutes, scan)
    assert record["hour_timestamp"] == "2026-03-09T16:00:00+00:00"
    assert record["scan_timestamp"] == "2026-03-09T17:02:00+00:00"
    assert record["observed_minutes"] == 60
    assert record["expected_minutes"] == 60
    assert record["coverage_ratio"] == 1.0
    assert record["last_price"] == 159.5
    assert record["minute_volume"] == 60.0


def test_minute_at_or_after_the_scan_is_excluded():
    hour = pd.Timestamp("2026-03-09T17:00:00Z")
    scan = pd.Timestamp("2026-03-09T17:02:00Z")
    frame = pd.DataFrame(
        {
            "open": [1.0, 2.0, 9.0],
            "high": [1.0, 2.0, 9.0],
            "low": [1.0, 2.0, 9.0],
            "close": [1.0, 2.0, 9.0],
            "volume": [1.0, 1.0, 100.0],
        },
        index=pd.to_datetime(
            ["2026-03-09T17:00:00Z", "2026-03-09T17:02:00Z", "2026-03-09T17:03:00Z"],
            utc=True,
        ),
    )
    record = enrich_hourly_bar("AAPL", scan, hour, 1.0, 2.0, frame, scan)
    assert record["observed_minutes"] == 1
    assert record["last_price"] == 1.0
    assert record["minute_volume"] == 1.0


def test_stale_hourly_bar_is_not_used():
    hour = pd.Timestamp("2026-03-09T16:00:00Z")
    scan = hour + pd.Timedelta(hours=2, minutes=1)
    hourly = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1.0]},
        index=pd.DatetimeIndex([hour]),
    )
    provider = _Provider({"AAPL": _minutes(hour, 60)})
    records = enrich_candidates(
        provider,
        [{"symbol": "AAPL", "scan_timestamp": scan.isoformat(), "veto_status": "PASS", "rank": 1}],
        {"AAPL": hourly},
    )
    assert provider.calls == []
    assert records[0]["hour_timestamp"] is None
    assert records[0]["observed_minutes"] is None


def test_only_selected_candidates_request_one_minute_bars():
    hour = pd.Timestamp("2026-10-02 18:00", tz="UTC")
    scan = hour + pd.Timedelta(hours=1)
    minutes = _minutes(hour, 60)

    def hourly(symbol, close):
        return pd.DataFrame(
            {"open": [100.0], "high": [close], "low": [100.0], "close": [close], "volume": [1.0]},
            index=pd.DatetimeIndex([hour]),
        )

    provider = _Provider({"PASS1": minutes, "PASS2": minutes, "BLOCKED": minutes})
    candidates = [
        {"symbol": "PASS2", "scan_timestamp": scan.isoformat(), "veto_status": "PASS", "rank": 2, "final_score": 8.0},
        {"symbol": "PASS1", "scan_timestamp": scan.isoformat(), "veto_status": "PASS", "rank": 1, "final_score": 9.0},
    ]
    records = enrich_candidates(
        provider,
        candidates,
        {"PASS1": hourly("PASS1", 110.0), "PASS2": hourly("PASS2", 90.0), "BLOCKED": hourly("BLOCKED", 120.0)},
    )
    assert [call["symbol"] for call in provider.calls] == ["PASS2", "PASS1"]
    assert {call["interval"] for call in provider.calls} == {"1m"}
    by_symbol = {row["symbol"]: row for row in records}
    assert by_symbol["PASS1"]["hourly_signal"] == "UP"
    assert by_symbol["PASS2"]["hourly_signal"] == "DOWN"
    assert "final_score" not in by_symbol["PASS1"]
    assert "BLOCKED" not in by_symbol

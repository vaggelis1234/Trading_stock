import sqlite3
from datetime import datetime, timedelta, timezone

from src.persistence.store import (
    count_snapshots,
    load_latest_scan,
    load_previous_ranks,
    load_symbol_history,
    save_universe_snapshots,
)


def _snapshot(symbol, stamp, status, reasons, score=None, rank=None):
    return {
        "scan_timestamp": stamp,
        "symbol": symbol,
        "hourly_close": 100.0,
        "daily_close": 100.0,
        "daily_ema200": 90.0,
        "rsi14": 50.0,
        "macd_hist": 0.1,
        "atr14": 1.0,
        "atr_percent": 0.01,
        "volume": 1000.0,
        "volume_ratio": 1.1,
        "adv20": 8_000_000.0,
        "veto_status": status,
        "veto_reasons": reasons,
        "earnings_check": "unavailable",
        "trend_score": score,
        "momentum_score": score,
        "volatility_score": score,
        "volume_liquidity_score": score,
        "final_score": score,
        "rank": rank,
    }


def test_snapshots_append_and_keep_veto_reasons(tmp_path):
    db = tmp_path / "universe.db"
    first = "2024-06-03T18:00:00+00:00"
    second = "2024-06-03T19:00:00+00:00"
    save_universe_snapshots(
        [_snapshot("AAPL", first, "BLOCKED_RSI", ["BLOCKED_RSI", "BLOCKED_VOLUME"])],
        str(db),
    )
    replaced = _snapshot("AAPL", first, "PASS", [], score=8.0, rank=1)
    inserted = save_universe_snapshots([replaced], str(db))
    assert inserted == 0

    save_universe_snapshots(
        [_snapshot("AAPL", second, "BLOCKED_VOLUME", ["BLOCKED_VOLUME", "BLOCKED_LIQUIDITY"])],
        str(db),
    )
    assert count_snapshots(str(db), "AAPL") == 2

    history = load_symbol_history(str(db), "AAPL", limit=24)
    assert [row["scan_timestamp"] for row in history] == [first, second]
    assert history[0]["veto_status"] == "BLOCKED_RSI"
    assert history[0]["veto_reasons"] == ["BLOCKED_RSI", "BLOCKED_VOLUME"]
    assert history[1]["veto_reasons"] == ["BLOCKED_VOLUME", "BLOCKED_LIQUIDITY"]

    latest = load_latest_scan(str(db))
    assert len(latest) == 1
    assert latest[0]["scan_timestamp"] == second
    assert latest[0]["veto_status"] == "BLOCKED_VOLUME"


def test_symbol_history_returns_the_latest_24_in_chronological_order(tmp_path):
    db = tmp_path / "universe.db"
    rows = []
    for hour in range(30):
        stamp = f"2024-06-01T{hour:02d}:00:00+00:00" if hour < 24 else f"2024-06-02T{hour - 24:02d}:00:00+00:00"
        rows.append(_snapshot("MSFT", stamp, "PASS", [], score=1.0, rank=1))
    save_universe_snapshots(rows, str(db))
    history = load_symbol_history(str(db), "MSFT", limit=24)
    assert len(history) == 24
    assert history[0]["scan_timestamp"] == "2024-06-01T06:00:00+00:00"
    assert history[-1]["scan_timestamp"] == "2024-06-02T05:00:00+00:00"
    assert count_snapshots(str(db)) == 30


def test_symbol_history_since_keeps_the_requested_window(tmp_path):
    db = tmp_path / "universe.db"
    stamps = [
        "2024-06-01T18:00:00+00:00",
        "2024-06-02T18:00:00+00:00",
        "2024-06-03T18:00:00+00:00",
    ]
    save_universe_snapshots(
        [_snapshot("AAPL", stamp, "PASS", [], score=1.0, rank=index) for index, stamp in enumerate(stamps, start=1)],
        str(db),
    )
    history = load_symbol_history(str(db), "AAPL", since="2024-06-02T18:00:00+00:00")
    assert [row["scan_timestamp"] for row in history] == stamps[1:]


def test_previous_rank_skips_an_unranked_snapshot(tmp_path):
    db = tmp_path / "universe.db"
    save_universe_snapshots(
        [
            _snapshot("AAPL", "2024-06-03T16:00:00+00:00", "PASS", [], score=8.0, rank=50),
            _snapshot("AAPL", "2024-06-03T17:00:00+00:00", "BLOCKED_INSUFFICIENT_DATA", ["BLOCKED_INSUFFICIENT_DATA"]),
            _snapshot("AAPL", "2024-06-03T18:00:00+00:00", "BLOCKED_VOLUME", ["BLOCKED_VOLUME"], score=9.0, rank=30),
            _snapshot("MSFT", "2024-06-03T18:00:00+00:00", "PASS", [], score=7.0, rank=2),
        ],
        str(db),
    )
    previous = load_previous_ranks(str(db), "2024-06-03T18:00:00+00:00")
    assert previous["AAPL"] == 50
    assert "MSFT" not in previous


def test_executed_at_is_distinct_from_market_data_timestamp(tmp_path):
    db = tmp_path / "universe.db"
    market = "2024-06-03T18:00:00+00:00"
    executed = "2026-09-29T21:05:00+00:00"
    save_universe_snapshots(
        [_snapshot("AAPL", market, "PASS", [], score=8.0, rank=1)],
        str(db),
        executed_at=executed,
    )
    latest = load_latest_scan(str(db))
    assert latest[0]["scan_timestamp"] == market
    assert latest[0]["executed_at"] == executed
    assert latest[0]["executed_at"] != latest[0]["scan_timestamp"]

    again = "2026-09-29T22:00:00+00:00"
    inserted = save_universe_snapshots(
        [_snapshot("AAPL", market, "PASS", [], score=1.0, rank=9)],
        str(db),
        executed_at=again,
    )
    assert inserted == 0
    assert load_latest_scan(str(db))[0]["executed_at"] == executed


def test_save_records_wall_clock_execution_time(tmp_path):
    db = tmp_path / "universe.db"
    market = "2024-06-03T18:00:00+00:00"
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    save_universe_snapshots(
        [_snapshot("MSFT", market, "PASS", [], score=3.0, rank=1)],
        str(db),
    )
    after = datetime.now(timezone.utc) + timedelta(seconds=2)
    latest = load_latest_scan(str(db))[0]
    executed = datetime.fromisoformat(latest["executed_at"])
    assert latest["scan_timestamp"] == market
    assert latest["executed_at"] != market
    assert before <= executed <= after


def test_existing_database_gains_executed_at_without_rewriting_old_rows(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE universe_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            hourly_close REAL,
            daily_close REAL,
            daily_ema200 REAL,
            rsi14 REAL,
            macd_hist REAL,
            atr14 REAL,
            atr_percent REAL,
            volume REAL,
            volume_ratio REAL,
            adv20 REAL,
            veto_status TEXT NOT NULL,
            veto_reasons TEXT NOT NULL,
            earnings_check TEXT NOT NULL,
            trend_score REAL,
            momentum_score REAL,
            volatility_score REAL,
            volume_liquidity_score REAL,
            final_score REAL,
            rank INTEGER,
            UNIQUE(scan_timestamp, symbol)
        )"""
    )
    conn.execute(
        """INSERT INTO universe_snapshots
           (scan_timestamp, symbol, veto_status, veto_reasons, earnings_check, final_score, rank)
           VALUES ('2024-06-03T17:00:00+00:00', 'AAPL', 'PASS', '[]', 'unavailable', 5.0, 1)"""
    )
    conn.commit()
    conn.close()

    save_universe_snapshots(
        [_snapshot("AAPL", "2024-06-03T18:00:00+00:00", "PASS", [], score=6.0, rank=1)],
        str(db),
        executed_at="2026-09-29T21:10:00+00:00",
    )
    history = load_symbol_history(str(db), "AAPL", limit=24)
    assert history[0]["executed_at"] is None
    assert history[0]["scan_timestamp"] == "2024-06-03T17:00:00+00:00"
    assert history[1]["executed_at"] == "2026-09-29T21:10:00+00:00"
    assert history[1]["executed_at"] != history[1]["scan_timestamp"]

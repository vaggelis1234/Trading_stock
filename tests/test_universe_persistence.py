import sqlite3
from datetime import datetime, timedelta, timezone

from src.persistence.store import (
    SNAPSHOT_CURRENT,
    SNAPSHOT_LOOKBACK,
    count_snapshots,
    load_latest_scan,
    load_previous_operational_run,
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


def test_legacy_rows_keep_null_run_identity(tmp_path):
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
            executed_at TEXT,
            UNIQUE(scan_timestamp, symbol)
        )"""
    )
    conn.execute(
        """INSERT INTO universe_snapshots
           (scan_timestamp, symbol, veto_status, veto_reasons, earnings_check, final_score, rank, executed_at)
           VALUES ('2026-03-09T16:30:00+00:00', 'AAPL', 'PASS', '[]', 'unavailable', 5.0, 4, '2026-03-09T16:30:08+00:00')"""
    )
    conn.commit()
    conn.close()

    save_universe_snapshots(
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00", "PASS", [], score=6.0, rank=1)],
        str(db),
        run_id="run-new",
        snapshot_kind=SNAPSHOT_CURRENT,
    )
    history = load_symbol_history(str(db), "AAPL", limit=10)
    assert history[0]["run_id"] is None
    assert history[0]["snapshot_kind"] is None
    assert history[0]["final_score"] == 5.0
    assert history[1]["run_id"] == "run-new"
    assert history[1]["snapshot_kind"] == SNAPSHOT_CURRENT
    assert load_previous_operational_run(str(db), "2026-03-09T17:02:00+00:00") is None


def test_previous_operational_run_skips_same_run_lookback(tmp_path):
    db = tmp_path / "universe.db"
    earlier = "2026-03-09T16:02:00+00:00"
    lookback = "2026-03-09T17:00:00+00:00"
    current = "2026-03-09T17:02:00+00:00"
    save_universe_snapshots(
        [_snapshot("AAPL", "2026-03-09T16:00:00+00:00", "PASS", [], score=7.0, rank=2)],
        str(db),
        run_id="run-a",
        snapshot_kind=SNAPSHOT_LOOKBACK,
    )
    save_universe_snapshots(
        [_snapshot("AAPL", earlier, "PASS", [], score=7.2, rank=2)],
        str(db),
        run_id="run-a",
        snapshot_kind=SNAPSHOT_CURRENT,
    )
    save_universe_snapshots(
        [_snapshot("AAPL", lookback, "PASS", [], score=8.0, rank=1)],
        str(db),
        run_id="run-b",
        snapshot_kind=SNAPSHOT_LOOKBACK,
    )
    save_universe_snapshots(
        [_snapshot("AAPL", current, "PASS", [], score=8.4, rank=1)],
        str(db),
        run_id="run-b",
        snapshot_kind=SNAPSHOT_CURRENT,
    )

    history = load_symbol_history(str(db), "AAPL", limit=10)
    kinds = {row["scan_timestamp"]: row["snapshot_kind"] for row in history}
    assert kinds[current] == SNAPSHOT_CURRENT
    assert kinds[lookback] == SNAPSHOT_LOOKBACK
    assert {row["run_id"] for row in history if row["scan_timestamp"] in {lookback, current}} == {"run-b"}

    conn = sqlite3.connect(db)
    naive = conn.execute(
        "SELECT MAX(scan_timestamp) FROM universe_snapshots WHERE scan_timestamp < ?",
        (current,),
    ).fetchone()[0]
    conn.close()
    assert naive == lookback

    previous = load_previous_operational_run(str(db), current)
    assert previous == {"run_id": "run-a", "scan_timestamp": earlier}


def test_movement_compares_with_previous_operational_current(tmp_path):
    db = tmp_path / "universe.db"
    path = str(db)

    def put(run_id, kind, stamp, ranked):
        save_universe_snapshots(
            [_snapshot(symbol, stamp, "PASS", [], score=1.0, rank=rank) for symbol, rank in ranked],
            path,
            run_id=run_id,
            snapshot_kind=kind,
        )

    put("run-a", SNAPSHOT_LOOKBACK, "2026-03-09T15:00:00+00:00", [("AAPL", 4), ("MSFT", 2)])
    put("run-a", SNAPSHOT_LOOKBACK, "2026-03-09T16:00:00+00:00", [("AAPL", 4), ("MSFT", 2)])
    put("run-a", SNAPSHOT_LOOKBACK, "2026-03-09T17:00:00+00:00", [("AAPL", 6), ("MSFT", 3)])
    put("run-a", SNAPSHOT_CURRENT, "2026-03-09T17:02:00+00:00", [("AAPL", 5), ("MSFT", 1), ("POOL", None)])
    put("run-b", SNAPSHOT_LOOKBACK, "2026-03-09T16:00:00+00:00", [("AAPL", 1)])
    put("run-b", SNAPSHOT_LOOKBACK, "2026-03-09T17:00:00+00:00", [("AAPL", 1)])
    put("run-b", SNAPSHOT_LOOKBACK, "2026-03-09T18:00:00+00:00", [("AAPL", 2), ("MSFT", 9), ("NEW", 4), ("POOL", 7)])
    put("run-b", SNAPSHOT_CURRENT, "2026-03-09T18:02:00+00:00", [("AAPL", 8), ("MSFT", 1), ("NEW", 4), ("POOL", 6)])

    current = "2026-03-09T18:02:00+00:00"
    previous = load_previous_ranks(path, current)
    assert previous == {"AAPL": 5, "MSFT": 1}

    from src.ui.universe_view import latest_table

    table = latest_table(load_latest_scan(path), previous).set_index("Symbol")
    assert table.loc["AAPL", "Movement"] == "🔴 ↓3"
    assert table.loc["MSFT", "Movement"] == "—"
    assert table.loc["NEW", "Movement"] == "NEW"
    assert table.loc["POOL", "Movement"] == "NEW"


def test_identified_run_ignores_legacy_rows_for_movement(tmp_path):
    db = tmp_path / "universe.db"
    save_universe_snapshots(
        [_snapshot("AAPL", "2026-03-09T16:00:00+00:00", "PASS", [], score=8.0, rank=50)],
        str(db),
    )
    save_universe_snapshots(
        [_snapshot("AAPL", "2026-03-09T17:00:00+00:00", "PASS", [], score=8.0, rank=3)],
        str(db),
        run_id="run-b",
        snapshot_kind=SNAPSHOT_LOOKBACK,
    )
    save_universe_snapshots(
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00", "PASS", [], score=8.0, rank=1)],
        str(db),
        run_id="run-b",
        snapshot_kind=SNAPSHOT_CURRENT,
    )
    assert load_previous_ranks(str(db), "2026-03-09T17:02:00+00:00") == {}

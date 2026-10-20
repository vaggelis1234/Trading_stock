"""Read-only Universe API. No market-data provider and no network.

The app is called in-process through its ASGI interface. Starlette's
TestClient is not imported: this environment has neither ``httpx`` nor
``httpx2``, and those packages are not project dependencies.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import anyio
import pytest

from src.ui.app import app
from src.data.minute_enrichment import ENRICHMENT_FIELDS, save_minute_enrichment
from src.persistence.store import SNAPSHOT_CURRENT, SNAPSHOT_LOOKBACK, save_universe_snapshots
from src.ui.api.db import READ_TIMEOUT_SECONDS, connect_readonly
from src.universe.veto import BLOCKED_VOLUME


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _get(path: str, params: dict | None = None) -> _Response:
    query = urlencode([(key, value) for key, value in (params or {}).items()])
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(b"host", b"127.0.0.1:8000")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    async def run():
        await app(scope, receive, send)

    anyio.run(run)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return _Response(status, json.loads(body) if body else None)


class _Client:
    def get(self, path: str, params: dict | None = None) -> _Response:
        return _get(path, params)


client = _Client()


def _snapshot(symbol, stamp, status="PASS", reasons=None, score=5.0, rank=1, **overrides):
    row = {
        "scan_timestamp": stamp,
        "symbol": symbol,
        "hourly_close": 100.0,
        "daily_close": 100.0,
        "daily_ema200": 90.0,
        "rsi14": 50.0,
        "macd_hist": 0.1,
        "atr14": 1.0,
        "atr_percent": 0.018,
        "volume": 1000.0,
        "volume_ratio": 1.1,
        "adv20": 8_000_000.0,
        "veto_status": status,
        "veto_reasons": [] if reasons is None else reasons,
        "earnings_check": "unavailable",
        "trend_score": score,
        "momentum_score": score,
        "volatility_score": score,
        "volume_liquidity_score": score,
        "final_score": score,
        "rank": rank,
    }
    row.update(overrides)
    return row


def _put(path, rows, run_id=None, kind=None, executed_at=None):
    save_universe_snapshots(rows, path, executed_at=executed_at, run_id=run_id, snapshot_kind=kind)


def _use(monkeypatch, path):
    monkeypatch.setenv("UNIVERSE_DB", str(path))


def _files(directory: Path) -> dict:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()}


def test_readonly_connection_rejects_writes_and_sets_timeout(tmp_path):
    db = tmp_path / "universe.db"
    _put(str(db), [_snapshot("AAPL", "2026-03-09T17:02:00+00:00")], run_id="run-a", kind=SNAPSHOT_CURRENT)
    with connect_readonly(str(db)) as connection:
        timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == int(READ_TIMEOUT_SECONDS * 1000)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE should_fail (id INTEGER)")


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "absent" / "universe.db"
    _use(monkeypatch, missing)
    response = client.get("/api/v1/scan/latest")
    assert response.status_code == 404
    assert response.json() == {"error": "no_snapshots"}
    assert not missing.exists()


def test_empty_database_has_no_operational_scan(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    _use(monkeypatch, db)
    assert client.get("/api/v1/scan/latest").json() == {"error": "no_snapshots"}
    assert client.get("/api/v1/symbols").json() == {"error": "no_snapshots"}
    assert client.get("/api/v1/symbols/AAPL").json() == {"error": "no_snapshots"}
    assert db.read_bytes() == before


def test_lookback_is_not_the_latest_scan_when_its_timestamp_is_greater(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    _put(
        path,
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00", score=5.0, rank=1)],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
        executed_at="2026-03-09T17:02:08+00:00",
    )
    _put(
        path,
        [_snapshot("AAPL", "2026-03-09T19:00:00+00:00", score=9.0, rank=1)],
        run_id="run-b",
        kind=SNAPSHOT_LOOKBACK,
    )
    _use(monkeypatch, db)
    body = client.get("/api/v1/scan/latest").json()
    assert body["scan_timestamp"] == "2026-03-09T17:02:00+00:00"
    assert body["run_id"] == "run-a"
    assert body["snapshot_kind"] == "CURRENT"
    assert body["executed_at"] == "2026-03-09T17:02:08+00:00"
    assert body["rows"][0]["score"] == 5.0
    assert body["rows"][0]["atr_percent"] == pytest.approx(1.8)


def test_current_scan_wins_over_a_later_legacy_row(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    _put(
        path,
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00", score=6.0, rank=1)],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
    )
    connection = sqlite3.connect(path)
    connection.execute(
        """INSERT INTO universe_snapshots
           (scan_timestamp, symbol, veto_status, veto_reasons, earnings_check, final_score, rank, run_id, snapshot_kind)
           VALUES ('2026-03-09T20:00:00+00:00', 'MSFT', 'PASS', '[]', 'unavailable', 9.0, 1, NULL, NULL)"""
    )
    connection.commit()
    connection.close()
    _use(monkeypatch, db)
    body = client.get("/api/v1/scan/latest").json()
    assert body["scan_timestamp"] == "2026-03-09T17:02:00+00:00"
    assert body["run_id"] == "run-a"
    assert [row["symbol"] for row in body["rows"]] == ["AAPL"]


def test_legacy_schema_without_identity_columns(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
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
    connection.execute(
        """INSERT INTO universe_snapshots
           (scan_timestamp, symbol, hourly_close, rsi14, atr_percent, volume_ratio, adv20,
            veto_status, veto_reasons, earnings_check, final_score, rank, executed_at)
           VALUES ('2026-03-09T16:00:00+00:00', 'MSFT', 90, 40, 0.02, 1, 8000000,
                   'PASS', '[]', 'unavailable', 4, 2, '2026-03-09T16:00:05+00:00')"""
    )
    connection.execute(
        """INSERT INTO universe_snapshots
           (scan_timestamp, symbol, hourly_close, rsi14, atr_percent, volume_ratio, adv20,
            veto_status, veto_reasons, earnings_check, final_score, rank, executed_at)
           VALUES ('2026-03-09T16:30:00+00:00', 'AAPL', NULL, NULL, NULL, NULL, NULL,
                   'BLOCKED_VOLUME', '["BLOCKED_VOLUME"]', 'unavailable', NULL, NULL,
                   '2026-03-09T16:30:08+00:00')"""
    )
    connection.commit()
    connection.close()
    _use(monkeypatch, db)
    body = client.get("/api/v1/scan/latest").json()
    assert body["scan_timestamp"] == "2026-03-09T16:30:00+00:00"
    assert body["run_id"] is None
    assert body["snapshot_kind"] is None
    assert body["evaluated"] == 1
    assert body["scored"] == 0
    assert body["rows"] == []
    search = client.get("/api/v1/symbols").json()
    assert search["total"] == 1
    row = search["rows"][0]
    assert row["symbol"] == "AAPL"
    assert row["price"] is None
    assert row["score"] is None
    assert row["rsi14"] is None
    assert row["atr_percent"] is None
    assert row["movement"] is None
    assert row["movement_label"] is None
    assert row["veto_reasons"] == ["BLOCKED_VOLUME"]
    assert row["status"] == "BLOCKED"


def test_legacy_null_identity_when_no_current_exists(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    _put(str(db), [_snapshot("AAPL", "2026-03-09T16:30:00+00:00", score=5.0, rank=4)])
    _put(
        str(db),
        [_snapshot("MSFT", "2026-03-09T18:00:00+00:00", score=9.0, rank=1)],
        run_id="run-b",
        kind=SNAPSHOT_LOOKBACK,
    )
    _use(monkeypatch, db)
    body = client.get("/api/v1/scan/latest").json()
    assert body["scan_timestamp"] == "2026-03-09T16:30:00+00:00"
    assert body["run_id"] is None
    assert body["snapshot_kind"] is None
    assert body["rows"][0]["symbol"] == "AAPL"


def test_lookback_only_database_is_not_a_scan(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    _put(
        str(db),
        [_snapshot("AAPL", "2026-03-09T19:00:00+00:00")],
        run_id="run-b",
        kind=SNAPSHOT_LOOKBACK,
    )
    _use(monkeypatch, db)
    assert client.get("/api/v1/scan/latest").status_code == 404
    assert client.get("/api/v1/symbols/AAPL").json() == {"error": "no_snapshots"}


def test_movement_uses_previous_operational_current(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)

    def put(run_id, kind, stamp, ranked):
        _put(
            path,
            [_snapshot(symbol, stamp, score=1.0, rank=rank) for symbol, rank in ranked],
            run_id=run_id,
            kind=kind,
        )

    put("run-a", SNAPSHOT_LOOKBACK, "2026-03-09T15:00:00+00:00", [("AAPL", 4), ("MSFT", 2)])
    put("run-a", SNAPSHOT_CURRENT, "2026-03-09T17:02:00+00:00", [("AAPL", 5), ("MSFT", 1), ("POOL", None)])
    put("run-b", SNAPSHOT_LOOKBACK, "2026-03-09T18:00:00+00:00", [("AAPL", 2), ("MSFT", 9), ("NEW", 4), ("POOL", 7)])
    put("run-b", SNAPSHOT_CURRENT, "2026-03-09T18:02:00+00:00", [("AAPL", 8), ("MSFT", 1), ("NEW", 4), ("POOL", 6)])
    _use(monkeypatch, db)
    rows = {row["symbol"]: row for row in client.get("/api/v1/scan/latest").json()["rows"]}
    assert rows["AAPL"]["previous_rank"] == 5
    assert rows["AAPL"]["movement"] == -3
    assert rows["AAPL"]["movement_label"] == "down"
    assert rows["MSFT"]["movement"] == 0
    assert rows["MSFT"]["movement_label"] == "flat"
    assert rows["NEW"]["movement_label"] == "new"
    assert rows["NEW"]["previous_rank"] is None
    assert rows["POOL"]["movement_label"] == "new"


def test_identified_scan_ignores_legacy_ranks_for_movement(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    _put(path, [_snapshot("AAPL", "2026-03-09T16:00:00+00:00", score=8.0, rank=50)])
    _put(
        path,
        [_snapshot("AAPL", "2026-03-09T17:00:00+00:00", score=8.0, rank=3)],
        run_id="run-b",
        kind=SNAPSHOT_LOOKBACK,
    )
    _put(
        path,
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00", score=8.0, rank=1)],
        run_id="run-b",
        kind=SNAPSHOT_CURRENT,
    )
    _use(monkeypatch, db)
    row = client.get("/api/v1/scan/latest").json()["rows"][0]
    assert row["movement_label"] == "new"
    assert row["previous_rank"] is None


def test_latest_scan_counts_and_top_100_only(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    stamp = "2026-03-09T18:02:00+00:00"
    rows = []
    for index in range(1, 106):
        rows.append(_snapshot(f"S{index:03d}", stamp, score=10 - index / 100, rank=index))
    rows.append(_snapshot("ZZZ", stamp, status="BLOCKED_VOLUME", reasons=["BLOCKED_VOLUME"], score=None, rank=None))
    _put(str(db), rows, run_id="run-a", kind=SNAPSHOT_CURRENT)
    _use(monkeypatch, db)
    body = client.get("/api/v1/scan/latest").json()
    assert body["evaluated"] == 106
    assert body["scored"] == 105
    assert body["passed"] == 105
    assert body["blocked"] == 1
    assert body["earnings_pending"] is True
    assert len(body["rows"]) == 100
    assert body["rows"][0]["rank"] == 1
    assert body["rows"][-1]["rank"] == 100
    assert "S101" not in {row["symbol"] for row in body["rows"]}
    assert "ZZZ" not in {row["symbol"] for row in body["rows"]}


def test_search_filters_and_paginates_the_full_scan(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    stamp = "2026-03-09T18:02:00+00:00"
    rows = [_snapshot(f"S{index:03d}", stamp, score=float(index), rank=index) for index in range(1, 121)]
    rows.append(
        _snapshot("AAA", stamp, status="BLOCKED_VOLUME", reasons=["BLOCKED_VOLUME"], score=9.5, rank=200)
    )
    rows.append(_snapshot("AAB", stamp, score=None, rank=None, rsi14=None, hourly_close=None, atr_percent=None))
    _put(str(db), rows, run_id="run-a", kind=SNAPSHOT_CURRENT)
    _use(monkeypatch, db)

    page = client.get("/api/v1/symbols", params={"limit": 10, "offset": 100}).json()
    assert page["total"] == 122
    assert page["limit"] == 10
    assert page["offset"] == 100
    assert [row["rank"] for row in page["rows"]] == list(range(101, 111))

    blocked = client.get("/api/v1/symbols", params={"status": "BLOCKED"}).json()
    assert blocked["total"] == 1
    assert blocked["rows"][0]["symbol"] == "AAA"
    assert blocked["rows"][0]["veto_reasons"] == ["BLOCKED_VOLUME"]
    assert blocked["rows"][0]["score"] == 9.5

    scored = client.get("/api/v1/symbols", params={"q": "aa", "min_score": 9}).json()
    assert [row["symbol"] for row in scored["rows"]] == ["AAA"]

    empty_score = client.get("/api/v1/symbols", params={"q": "AAB"}).json()["rows"][0]
    assert empty_score["score"] is None
    assert empty_score["rsi14"] is None
    assert empty_score["price"] is None
    assert empty_score["atr_percent"] is None

    assert client.get("/api/v1/symbols", params={"status": "BLOCKED_VOLUME"}).status_code == 422
    assert client.get("/api/v1/symbols", params={"limit": 0}).status_code == 422


def test_unknown_symbol(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    _put(
        str(db),
        [_snapshot("AAPL", "2026-03-09T17:02:00+00:00")],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
    )
    _use(monkeypatch, db)
    response = client.get("/api/v1/symbols/MSFT")
    assert response.status_code == 404
    assert response.json() == {"error": "symbol_not_found"}


def test_symbol_history_covers_1702_scan_as_the_1700_hour(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    null_hour = "2026-10-09T15:00:00+00:00"
    lookback = "2026-10-09T16:00:00+00:00"
    current = "2026-10-09T17:02:00+00:00"
    _put(
        path,
        [_snapshot("AAPL", null_hour, score=4.0, rank=3, rsi14=None, atr_percent=None)],
        run_id="run-a",
        kind=SNAPSHOT_LOOKBACK,
    )
    _put(
        path,
        [
            _snapshot(
                "AAPL",
                lookback,
                status="BLOCKED_VOLUME",
                reasons=[BLOCKED_VOLUME],
                score=4.5,
                rank=2,
            )
        ],
        run_id="run-a",
        kind=SNAPSHOT_LOOKBACK,
    )
    _put(
        path,
        [_snapshot("AAPL", current, score=6.0, rank=1)],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
        executed_at="2026-10-09T17:02:10+00:00",
    )
    _use(monkeypatch, db)
    body = client.get("/api/v1/symbols/aapl", params={"range": "24H"}).json()
    assert body["symbol"] == "AAPL"
    assert body["scan_timestamp"] == current
    assert body["snapshot_kind"] == "CURRENT"
    assert body["range"] == "24H"
    stamps = [row["scan_timestamp"] for row in body["history"]]
    assert stamps == [null_hour, lookback, current]
    assert body["history"][1]["snapshot_kind"] == "LOOKBACK"
    assert body["history"][1]["reasons"] == ["VolumeRatio < 0.8"]
    assert body["history"][0]["rsi14"] is None
    assert body["history"][0]["atr_percent"] is None
    coverage = body["coverage"]["RSI"]
    assert coverage["expected"][-1] == "2026-10-09T15:30:00+00:00"
    assert "2026-10-09T17:02:00+00:00" not in coverage["expected"]
    assert "2026-10-08T17:30:00+00:00" in coverage["expected"]
    assert "2026-10-09T14:30:00+00:00" in coverage["null_metric"]
    assert null_hour not in coverage["expected"]
    assert body["enrichment"] is None
    assert client.get("/api/v1/symbols/AAPL", params={"range": "2D"}).status_code == 422


def test_enrichment_is_returned_only_for_a_stored_row(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    current = "2026-10-09T17:02:00+00:00"
    _put(path, [_snapshot("AAPL", current, score=6.0, rank=1), _snapshot("MSFT", current, score=5.0, rank=2)], run_id="run-a", kind=SNAPSHOT_CURRENT)
    stored = {name: None for name in ENRICHMENT_FIELDS}
    stored.update(
        {
            "scan_timestamp": current,
            "symbol": "AAPL",
            "hour_timestamp": "2026-10-09T17:00:00+00:00",
            "session_type": "REGULAR",
            "expected_minutes": 60,
            "observed_minutes": 12,
            "coverage_ratio": 0.2,
            "first_price": 100.0,
            "last_price": 101.0,
        }
    )
    save_minute_enrichment(path, [stored])
    _use(monkeypatch, db)
    apple = client.get("/api/v1/symbols/AAPL").json()["enrichment"]
    assert apple["symbol"] == "AAPL"
    assert apple["observed_minutes"] == 12
    assert apple["return_5m"] is None
    assert client.get("/api/v1/symbols/MSFT").json()["enrichment"] is None


def test_reads_do_not_change_the_database(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    current = "2026-10-09T17:02:00+00:00"
    _put(
        str(db),
        [_snapshot("AAPL", current, score=6.0, rank=1)],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
    )
    before = _files(tmp_path)
    count_before = _count(db)
    _use(monkeypatch, db)
    assert client.get("/api/v1/scan/latest").status_code == 200
    assert client.get("/api/v1/symbols", params={"q": "AA"}).status_code == 200
    assert client.get("/api/v1/symbols/AAPL", params={"range": "5D"}).status_code == 200
    assert _files(tmp_path) == before
    assert _count(db) == count_before


def test_scores_route_still_reads_the_csv(tmp_path, monkeypatch):
    monkeypatch.setattr("src.ui.app.ARTIFACT_DIR", str(tmp_path))
    csv_path = tmp_path / "scores_20260101T000000Z.csv"
    csv_path.write_text("symbol,price,score,veto_status,rank\nAAPL,10.5,1.5,PASS,1\n", encoding="utf-8")
    found = client.get("/scores")
    assert found.status_code == 200
    assert found.json() == [
        {"symbol": "AAPL", "price": 10.5, "score": 1.5, "veto_status": "PASS", "rank": 1}
    ]
    csv_path.unlink()
    missing = client.get("/scores")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no scores yet"}


def test_json_contract_matches_frontend_types(tmp_path, monkeypatch):
    db = tmp_path / "universe.db"
    path = str(db)
    lookback = "2026-10-09T16:00:00+00:00"
    current = "2026-10-09T17:02:00+00:00"
    _put(
        path,
        [_snapshot("AAPL", lookback, status="BLOCKED_VOLUME", reasons=["BLOCKED_VOLUME"], score=4.5, rank=2, rsi14=None)],
        run_id="run-a",
        kind=SNAPSHOT_LOOKBACK,
    )
    _put(
        path,
        [_snapshot("AAPL", current, score=6.0, rank=1)],
        run_id="run-a",
        kind=SNAPSHOT_CURRENT,
        executed_at="2026-10-09T17:02:10+00:00",
    )
    _use(monkeypatch, db)
    latest = client.get("/api/v1/scan/latest").json()
    assert set(latest) == {
        "scan_timestamp",
        "executed_at",
        "run_id",
        "snapshot_kind",
        "evaluated",
        "scored",
        "passed",
        "blocked",
        "earnings_pending",
        "rows",
    }
    assert set(latest["rows"][0]) == {
        "rank",
        "symbol",
        "price",
        "status",
        "score",
        "movement",
        "movement_label",
        "previous_rank",
        "rsi14",
        "macd_hist",
        "atr_percent",
        "volume_ratio",
        "adv20",
        "veto_reasons",
    }
    search = client.get("/api/v1/symbols", params={"q": "AA", "limit": 25, "offset": 0}).json()
    assert set(search) == {
        "scan_timestamp",
        "run_id",
        "snapshot_kind",
        "total",
        "limit",
        "offset",
        "rows",
    }
    detail = client.get("/api/v1/symbols/AAPL", params={"range": "24H"}).json()
    assert detail["snapshot_kind"] == "CURRENT"
    assert detail["name"] == "Apple Inc."
    assert detail["history"][0]["snapshot_kind"] == "LOOKBACK"
    assert set(detail) == {
        "symbol",
        "name",
        "scan_timestamp",
        "executed_at",
        "run_id",
        "snapshot_kind",
        "range",
        "since",
        "detail",
        "enrichment",
        "history",
        "coverage",
    }
    assert set(detail["detail"]) == {
        "rank",
        "movement",
        "movement_label",
        "previous_rank",
        "score",
        "status",
        "reasons",
        "veto_reasons",
        "price",
        "rsi14",
        "macd_hist",
        "atr_percent",
        "volume_ratio",
        "adv20",
        "earnings_check",
    }
    assert detail["detail"]["rank"] == 1
    assert set(detail["history"][0]) == {
        "scan_timestamp",
        "run_id",
        "snapshot_kind",
        "status",
        "reasons",
        "veto_reasons",
        "price",
        "rsi14",
        "final_score",
        "atr_percent",
        "volume_ratio",
    }
    assert detail["history"][0]["price"] == 100.0
    assert set(detail["coverage"]) == {"RSI", "Score", "ATR%", "Volume Ratio"}
    assert set(detail["coverage"]["RSI"]) == {"expected", "actual", "missing", "null_metric"}
    assert detail["history"][0]["rsi14"] is None


def test_rules_are_the_hardcoded_scan_constants():
    body = _get("/api/v1/rules").json()
    groups = {group["title"]: group for group in body["groups"]}
    vetoes = {row["name"]: row["value"] for row in groups["Hard veto"]["rows"]}
    assert vetoes["RSI"] == "35 ≤ RSI(14) ≤ 75"
    assert vetoes["Liquidity"] == "ADV20 ≥ $5,000,000"
    windows = {row["name"]: row["value"] for row in groups["Session windows"]["rows"]}
    assert windows["SIP delay"] == "15 minutes after the window ends"
    assert windows["11:30–12:30"] == "Readable from 12:45"
    assert windows["12:30–13:30"] == "Readable from 13:45"
    assert windows["15:30–16:00"] == "Readable from 16:15"
    assert groups["Ranking"]["rows"][0]["value"] == "Top 100 by score"


def _count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        snapshots = connection.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        enrichment = 0
        if "minute_enrichment" in tables:
            enrichment = connection.execute("SELECT COUNT(*) FROM minute_enrichment").fetchone()[0]
        return int(snapshots) + int(enrichment)
    finally:
        connection.close()

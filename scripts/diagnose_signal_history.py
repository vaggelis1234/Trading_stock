"""Read-only history check for stored Universe decisions.

LOOKBACK rows are listed and then ignored. Returns use the snapshot price
stored at the scan and later completed Alpaca bars already on disk. The
script does not fetch, score, rank, or write.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market.calendar import NyseCalendar

HORIZONS = (1, 2, 3, 5)
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
LIVE_UNIVERSE = ROOT / "artifacts" / "universe.db"
BACKUP_UNIVERSE = ROOT / "artifacts" / "backups" / "2026-10-23-universe-30m" / "universe.db"
BARS = ROOT / "artifacts" / "market_bars.db"
RANK_BINS = ((1, 10), (11, 25), (26, 50), (51, 100), (101, 10_000))
VOLUME_BINS = ((0.8, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 1e9))


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _session_day(stamp: datetime) -> date:
    return stamp.astimezone(NY).date()


def _next_sessions(calendar: NyseCalendar, day: date, count: int) -> list[date]:
    found = []
    cursor = day + timedelta(days=1)
    while len(found) < count:
        if calendar.is_trading_day(cursor):
            found.append(cursor)
        cursor += timedelta(days=1)
    return found


def _load_decisions() -> list[dict]:
    rows = []
    for source, path in (("backup", BACKUP_UNIVERSE), ("live", LIVE_UNIVERSE)):
        connection = _connect(path)
        fetched = connection.execute(
            """SELECT scan_timestamp, executed_at, run_id, symbol, veto_status,
                      hourly_close, volume_ratio, final_score, rank
               FROM universe_snapshots
               WHERE snapshot_kind = 'CURRENT'"""
        ).fetchall()
        for row in fetched:
            item = dict(row)
            item["source"] = source
            item["stamp"] = _stamp(item["scan_timestamp"])
            item["session"] = _session_day(item["stamp"])
            rows.append(item)
        connection.close()
    return rows


def _load_daily(symbols: set[str]) -> dict[tuple[str, date], float]:
    connection = _connect(BARS)
    closes = {}
    query = """SELECT symbol, bar_timestamp, close
               FROM market_bars
               WHERE interval = '1d' AND bar_timestamp >= '2026-10-19' AND bar_timestamp < '2026-10-23'"""
    for row in connection.execute(query):
        if row["symbol"] not in symbols or row["close"] is None:
            continue
        closes[(row["symbol"], _session_day(_stamp(row["bar_timestamp"])))] = float(row["close"])
    connection.close()
    return closes


def _load_path(symbols: set[str]) -> dict[str, list[tuple[datetime, float, float]]]:
    connection = _connect(BARS)
    path = defaultdict(list)
    query = """SELECT symbol, bar_timestamp, high, low
               FROM market_bars
               WHERE interval = '30m'
                 AND bar_timestamp >= '2026-10-19T13:30:00+00:00'
                 AND bar_timestamp < '2026-10-22T20:00:00+00:00'"""
    for row in connection.execute(query):
        if row["symbol"] not in symbols or row["high"] is None or row["low"] is None:
            continue
        start = _stamp(row["bar_timestamp"])
        local = start.astimezone(NY)
        if local.hour < 9 or (local.hour == 9 and local.minute < 30) or local.hour > 15:
            continue
        path[row["symbol"]].append((start, float(row["high"]), float(row["low"])))
    connection.close()
    for bars in path.values():
        bars.sort()
    return path


def _entry_matches(symbols_rows: list[dict], scan: datetime) -> tuple[int, int]:
    """How many stored closes still equal a completed 30m close before the scan."""
    connection = _connect(BARS)
    window_start = (scan - timedelta(hours=2)).isoformat()
    completed_before = (scan - timedelta(minutes=30)).isoformat()
    found = {}
    for row in connection.execute(
        """SELECT symbol, close FROM market_bars
           WHERE interval = '30m' AND bar_timestamp >= ? AND bar_timestamp <= ?""",
        (window_start, completed_before),
    ):
        if row["close"] is None:
            continue
        found.setdefault(row["symbol"], []).append(float(row["close"]))
    connection.close()
    checked = matched = 0
    for row in symbols_rows:
        price = row["hourly_close"]
        if price is None:
            continue
        checked += 1
        for close in found.get(row["symbol"], []):
            if abs(close - float(price)) <= max(0.01, abs(float(price)) * 1e-4):
                matched += 1
                break
    return matched, checked


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "positive_pct": None, "mean_pct": None, "median_pct": None}
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[mid]
    else:
        median = (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(values),
        "positive_pct": round(100 * sum(value > 0 for value in values) / len(values), 1),
        "mean_pct": round(100 * sum(values) / len(values), 3),
        "median_pct": round(100 * median, 3),
    }


def _pair_stats(rows: list[dict], field: str) -> dict:
    return _stats([row[field] for row in rows if row.get(field) is not None])


def _evaluate(rows: list[dict], closes: dict, path: dict, sessions: list[date]) -> list[dict]:
    evaluated = []
    for row in rows:
        price = row["hourly_close"]
        if price is None or price <= 0:
            continue
        item = dict(row)
        entry = float(price)
        for horizon, target in enumerate(sessions, start=1):
            if horizon not in HORIZONS:
                continue
            exit_price = closes.get((row["symbol"], target))
            item[f"ret_{horizon}"] = None if exit_price is None else exit_price / entry - 1
            highs = []
            lows = []
            for start, high, low in path.get(row["symbol"], []):
                if start < row["stamp"]:
                    continue
                if _session_day(start) > target:
                    break
                highs.append(high)
                lows.append(low)
            reached = any(_session_day(start) == target for start, _, _ in path.get(row["symbol"], []) if start >= row["stamp"])
            if highs and reached and exit_price is not None:
                item[f"mfe_{horizon}"] = max(highs) / entry - 1
                item[f"mae_{horizon}"] = min(lows) / entry - 1
            else:
                item[f"mfe_{horizon}"] = None
                item[f"mae_{horizon}"] = None
        evaluated.append(item)
    return evaluated


def _print_block(title: str, rows: list[dict]) -> None:
    print(f"\n{title} n_rows={len(rows)}")
    for horizon in HORIZONS:
        usable = [row for row in rows if row.get(f"ret_{horizon}") is not None]
        ret = _stats([row[f"ret_{horizon}"] for row in usable])
        mfe = _stats([row[f"mfe_{horizon}"] for row in usable if row.get(f"mfe_{horizon}") is not None])
        mae = _stats([row[f"mae_{horizon}"] for row in usable if row.get(f"mae_{horizon}") is not None])
        print(f"  h{horizon} ret={ret} missing_ret={len(rows) - len(usable)} mfe={mfe} mae={mae}")


def _group(rows: list[dict], key) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return grouped


def main() -> None:
    calendar = NyseCalendar()
    decisions = _load_decisions()
    clocks = sorted({row["scan_timestamp"] for row in decisions})
    print("current_clocks")
    for clock in clocks:
        sample = [row for row in decisions if row["scan_timestamp"] == clock]
        head = sample[0]
        local = head["stamp"].astimezone(NY)
        statuses = defaultdict(int)
        for row in sample:
            statuses[row["veto_status"]] += 1
        cash = local.hour > 9 or (local.hour == 9 and local.minute >= 30)
        print(
            {
                "clock_utc": clock,
                "clock_et": local.strftime("%Y-%m-%d %H:%M"),
                "source": head["source"],
                "run_id": head["run_id"],
                "executed_at": head["executed_at"],
                "rows": len(sample),
                "cash_session": cash,
                "status": dict(statuses),
            }
        )

    cash_rows = []
    for row in decisions:
        local = row["stamp"].astimezone(NY)
        if local.hour > 9 or (local.hour == 9 and local.minute >= 30):
            cash_rows.append(row)
    symbols = {row["symbol"] for row in cash_rows}
    closes = _load_daily(symbols)
    path = _load_path(symbols)
    print("daily_sessions_loaded", sorted({day.isoformat() for _, day in closes}))
    print("path_symbols", len(path))

    by_clock = _group(cash_rows, lambda row: row["scan_timestamp"])
    evaluated = []
    for clock, rows in by_clock.items():
        sessions = _next_sessions(calendar, rows[0]["session"], max(HORIZONS))
        matched, checked = _entry_matches(rows, rows[0]["stamp"])
        print(
            "entry_matches_completed_30m",
            clock,
            {"matched": matched, "with_price": checked, "targets": [day.isoformat() for day in sessions]},
        )
        evaluated.extend(_evaluate(rows, closes, path, sessions))

    print("\n== per scan ==")
    for clock in sorted(by_clock):
        rows = [row for row in evaluated if row["scan_timestamp"] == clock]
        local = rows[0]["stamp"].astimezone(NY).strftime("%Y-%m-%d %H:%M ET")
        _print_block(f"ALL {local}", rows)
        _print_block(f"PASS {local}", [row for row in rows if row["veto_status"] == "PASS"])
        _print_block(f"BLOCKED {local}", [row for row in rows if row["veto_status"] != "PASS"])
        _print_block(
            f"BLOCKED_VOLUME {local}",
            [row for row in rows if row["veto_status"] == "BLOCKED_VOLUME"],
        )

    latest = {}
    for row in evaluated:
        if row["session"] >= date(2026, 10, 22):
            continue
        key = (row["symbol"], row["session"])
        current = latest.get(key)
        if current is None or row["stamp"] > current["stamp"]:
            latest[key] = row
    collapsed = list(latest.values())
    print("\n== one row per symbol per session, latest cash scan ==")
    _print_block("ALL collapsed", collapsed)
    _print_block("PASS collapsed", [row for row in collapsed if row["veto_status"] == "PASS"])
    _print_block("BLOCKED collapsed", [row for row in collapsed if row["veto_status"] != "PASS"])
    _print_block(
        "BLOCKED_VOLUME collapsed",
        [row for row in collapsed if row["veto_status"] == "BLOCKED_VOLUME"],
    )

    passed = [row for row in collapsed if row["veto_status"] == "PASS"]
    print("\n== PASS rank bins, collapsed ==")
    for lo, hi in RANK_BINS:
        chosen = [row for row in passed if row["rank"] is not None and lo <= row["rank"] <= hi]
        _print_block(f"rank {lo}-{hi}", chosen)
    print("\n== PASS score bins, collapsed ==")
    for lo, hi, label in ((0, 6, "<6"), (6, 7, "6-7"), (7, 8, "7-8"), (8, 100, ">=8")):
        chosen = [
            row
            for row in passed
            if row["final_score"] is not None and lo <= float(row["final_score"]) < hi
        ]
        _print_block(f"score {label}", chosen)
    print("\n== PASS volume-ratio bins, collapsed, threshold unchanged at 0.8 ==")
    for lo, hi in VOLUME_BINS:
        chosen = [
            row
            for row in passed
            if row["volume_ratio"] is not None and lo <= float(row["volume_ratio"]) < hi
        ]
        _print_block(f"volume_ratio [{lo}, {hi})", chosen)

    print("\n== PASS by scan clock ==")
    for clock in sorted({row["scan_timestamp"] for row in evaluated if row["veto_status"] == "PASS"}):
        rows = [row for row in evaluated if row["scan_timestamp"] == clock and row["veto_status"] == "PASS"]
        local = rows[0]["stamp"].astimezone(NY).strftime("%H:%M ET")
        _print_block(f"PASS {rows[0]['session']} {local}", rows)

    oct19 = [row for row in evaluated if row["session"].isoformat() == "2026-10-19" and row["veto_status"] == "PASS"]
    early = {row["symbol"] for row in oct19 if row["scan_timestamp"].startswith("2026-10-19T17:33")}
    late = {row["symbol"] for row in oct19 if row["scan_timestamp"].startswith("2026-10-19T19:06")}
    day20 = {
        row["symbol"]
        for row in evaluated
        if row["session"].isoformat() == "2026-10-20" and row["veto_status"] == "PASS"
    }
    print("\noverlap")
    print({"oct19_1733_pass": len(early), "oct19_1906_pass": len(late), "both_oct19": len(early & late)})
    print({"oct19_latest_pass": len(late), "oct20_pass": len(day20), "both_days": len(late & day20)})


if __name__ == "__main__":
    main()

from src.persistence.store import count_snapshots, load_latest_scan, load_symbol_history, save_universe_snapshots


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

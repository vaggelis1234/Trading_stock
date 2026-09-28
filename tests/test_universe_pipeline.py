import pandas as pd

from src.data.provider import YFinanceMarketDataProvider
from src.persistence.store import count_snapshots, load_latest_scan, load_symbol_history
from src.universe.pipeline import common_timestamps, run_universe


class MapProvider:
    def __init__(self, daily, hourly):
        self.daily = daily
        self.hourly = hourly

    def get_bars(self, symbol, start, end, interval):
        return self.daily[symbol] if interval == "1d" else self.hourly[symbol]


class AlwaysEarnings:
    available = True

    def within_hours(self, symbol, as_of, hours=48):
        return True


def _frames(direction):
    daily_idx = pd.bdate_range("2023-01-02", periods=250, tz="America/New_York")
    steps = pd.Series(range(250), dtype=float).to_numpy()
    if direction == "up":
        close = steps * 0.2 + 100.0
    else:
        close = steps * -0.2 + 160.0
    daily = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=daily_idx,
    )
    as_of = (daily_idx[-1] + pd.Timedelta(days=1, hours=16)).tz_convert("UTC").floor("min")
    hourly_idx = pd.date_range(as_of - pd.Timedelta(hours=79), periods=80, freq="h", tz="UTC")
    hourly_close = [140.4 if i % 2 == 0 else 139.6 for i in range(80)]
    hourly = pd.DataFrame(
        {
            "open": hourly_close,
            "high": [price + 0.15 for price in hourly_close],
            "low": [price - 0.15 for price in hourly_close],
            "close": hourly_close,
            "volume": 250_000.0,
        },
        index=hourly_idx,
    )
    return daily, hourly, as_of


def test_common_timestamps_use_the_shared_clock():
    idx = pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC")
    frames = {
        "A": pd.DataFrame({"close": 1.0}, index=idx),
        "B": pd.DataFrame({"close": 1.0}, index=idx[1:]),
    }
    stamps = common_timestamps(frames, 2)
    assert list(stamps) == list(idx[-2:])


def test_scan_persists_history_and_does_not_overwrite(tmp_path):
    up_daily, up_hourly, as_of = _frames("up")
    down_daily, down_hourly, _as_of = _frames("down")
    provider = MapProvider(
        {"UP": up_daily, "DOWN": down_daily},
        {"UP": up_hourly, "DOWN": down_hourly},
    )
    db = tmp_path / "universe.db"
    artifacts = tmp_path / "artifacts"
    rows = run_universe(
        ["UP", "DOWN"],
        provider=provider,
        db_path=str(db),
        artifact_dir=str(artifacts),
        lookback_bars=24,
        now=as_of,
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["UP"]["veto_status"] == "PASS", by_symbol["UP"]
    assert by_symbol["UP"]["rank"] == 1
    assert by_symbol["UP"]["final_score"] is not None
    assert 0.0 <= by_symbol["UP"]["final_score"] <= 10.0
    assert by_symbol["UP"]["earnings_check"] == "unavailable"
    assert "BLOCKED_EARNINGS" not in by_symbol["UP"]["veto_reasons"]
    assert by_symbol["DOWN"]["veto_status"] == "BLOCKED_TREND", by_symbol["DOWN"]
    assert "BLOCKED_TREND" in by_symbol["DOWN"]["veto_reasons"]
    assert by_symbol["DOWN"]["rank"] is None
    assert by_symbol["DOWN"]["final_score"] is None
    assert rows[0]["symbol"] == "UP"

    assert count_snapshots(str(db)) == 48
    run_universe(
        ["UP", "DOWN"],
        provider=provider,
        db_path=str(db),
        artifact_dir=str(artifacts),
        lookback_bars=24,
        now=as_of,
    )
    assert count_snapshots(str(db)) == 48

    history = load_symbol_history(str(db), "DOWN", limit=24)
    assert len(history) == 24
    assert history[-1]["veto_status"] == "BLOCKED_TREND"
    assert "BLOCKED_TREND" in history[-1]["veto_reasons"]
    latest = load_latest_scan(str(db))
    assert [row["symbol"] for row in latest] == ["UP", "DOWN"]


def test_earnings_provider_blocks_and_clears_the_score(tmp_path):
    daily, hourly, as_of = _frames("up")
    provider = MapProvider({"AAPL": daily}, {"AAPL": hourly})
    rows = run_universe(
        ["AAPL"],
        provider=provider,
        earnings=AlwaysEarnings(),
        db_path=str(tmp_path / "universe.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        lookback_bars=1,
        now=as_of,
    )
    assert rows[0]["veto_status"] == "BLOCKED_EARNINGS"
    assert rows[0]["veto_reasons"] == ["BLOCKED_EARNINGS"]
    assert rows[0]["earnings_check"] == "within_48h"
    assert rows[0]["final_score"] is None
    assert rows[0]["rank"] is None


def test_yfinance_provider_reuses_existing_fetch(monkeypatch):
    captured = {}

    def fake_fetch(symbol, start, end, interval="1d"):
        captured["args"] = (symbol, start, end, interval)
        index = pd.date_range("2024-01-02", periods=2, freq="D", tz="UTC")
        return pd.DataFrame(
            {
                "Open": [1, 2],
                "High": [1, 2],
                "Low": [1, 2],
                "Close": [1, 2],
                "Volume": [10, 10],
            },
            index=index,
        )

    monkeypatch.setattr("src.data.provider.fetch_historical_yfinance", fake_fetch)
    frame = YFinanceMarketDataProvider().get_bars("AAPL", "2024-01-01", "2024-02-01", "1h")
    assert captured["args"] == ("AAPL", "2024-01-01", "2024-02-01", "1h")
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[-1] == 2

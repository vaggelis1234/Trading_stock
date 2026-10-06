import logging

import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.minute_enrichment import enrich_candidates
from src.data.provider import AlpacaMarketDataProvider, MarketDataUnavailable, YFinanceMarketDataProvider
from src.persistence.store import count_snapshots, load_latest_scan, load_symbol_history
from src.universe.metrics import to_utc_iso
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
    assert by_symbol["DOWN"]["final_score"] is not None
    assert by_symbol["DOWN"]["rank"] is not None
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0]["final_score"] >= rows[1]["final_score"]
    assert {row["symbol"] for row in rows} == {"UP", "DOWN"}

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
    assert [row["symbol"] for row in latest] == [row["symbol"] for row in rows]
    assert latest[0]["rank"] == 1
    assert latest[1]["final_score"] is not None


def test_earnings_block_keeps_a_score_when_metrics_are_valid(tmp_path):
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
    assert rows[0]["final_score"] is not None
    assert 0.0 <= rows[0]["final_score"] <= 10.0
    assert rows[0]["rank"] == 1


def test_default_pool_is_the_full_sp500_list_not_market_cap(monkeypatch):
    monkeypatch.setattr("src.data.scan.fetch_sp500_symbols", lambda: ["ZZZ", "AAA", "MMM"])

    def fail_market_cap(*_args, **_kwargs):
        raise AssertionError("market cap sort must not build the evaluation pool")

    monkeypatch.setattr("src.data.scan.top_n_by_marketcap", fail_market_cap)
    from src.data.scan import build_sp500_universe

    assert build_sp500_universe() == ["ZZZ", "AAA", "MMM"]
    assert build_sp500_universe(limit=2) == ["ZZZ", "AAA"]

    monkeypatch.delenv("UNIVERSE_SIZE", raising=False)
    from scripts.run_universe import resolve_symbols
    from scripts.scheduler import resolve_symbols as scheduler_symbols

    assert resolve_symbols("", None) == ["ZZZ", "AAA", "MMM"]
    assert resolve_symbols("msft, aapl", 100) == ["MSFT", "AAPL"]
    assert resolve_symbols("", 1) == ["ZZZ"]
    monkeypatch.setenv("UNIVERSE_SIZE", "2")
    assert resolve_symbols("", None) == ["ZZZ", "AAA"]
    monkeypatch.delenv("UNIVERSE_SYMBOLS", raising=False)
    assert scheduler_symbols() == ["ZZZ", "AAA"]
    monkeypatch.setenv("UNIVERSE_SYMBOLS", "nvda, xom")
    assert scheduler_symbols() == ["NVDA", "XOM"]


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


def _alpaca_rows(frame):
    rows = []
    for stamp, row in frame.iterrows():
        ts = pd.Timestamp(stamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        rows.append(
            {
                "t": ts.isoformat().replace("+00:00", "Z"),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]),
            }
        )
    return rows


class _Payload:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_production_scan_defaults_to_alpaca_sip(monkeypatch, tmp_path, caplog):
    """A scan with no provider argument requests Alpaca SIP, not Yahoo."""
    daily, hourly, as_of = _frames("up")
    pages = {
        "1Day": _alpaca_rows(daily),
        "1Hour": _alpaca_rows(hourly),
        "1Min": [
            {
                "t": "2024-06-03T14:30:00Z",
                "o": 10.0,
                "h": 11.0,
                "l": 9.0,
                "c": 10.5,
                "v": 3.0,
            }
        ],
    }
    calls = []

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            query = dict(params or {})
            calls.append({"url": url, "params": query, "headers": dict(headers or {})})
            symbol = query.get("symbols")
            timeframe = query.get("timeframe")
            if symbol != "BRK.B":
                return _Payload({"bars": {}, "next_page_token": None})
            rows = pages[timeframe]
            token = query.get("page_token")
            if timeframe == "1Day" and not token:
                return _Payload({"bars": {"BRK.B": rows[:1]}, "next_page_token": "page-2"})
            if timeframe == "1Day":
                return _Payload({"bars": {"BRK.B": rows[1:]}, "next_page_token": None})
            return _Payload({"bars": {"BRK.B": rows}, "next_page_token": None})

    def yahoo_must_not_run(*_args, **_kwargs):
        raise AssertionError("production universe must not call yfinance")

    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "key-secret")
    monkeypatch.setattr("src.data.provider.requests.Session", Session)
    monkeypatch.setattr("src.data.provider.fetch_historical_yfinance", yahoo_must_not_run)
    monkeypatch.setattr("src.data.ingest.fetch_historical_yfinance", yahoo_must_not_run)

    captured = {}

    def spy(provider, candidates, hourly_by_symbol, bar_store=None):
        captured["provider"] = provider
        return enrich_candidates(provider, candidates, hourly_by_symbol, bar_store)

    monkeypatch.setattr("src.universe.pipeline.enrich_candidates", spy)

    bars_db = tmp_path / "market_bars.db"
    with caplog.at_level(logging.DEBUG):
        rows = run_universe(
            ["BRK-B", "ZZZ"],
            db_path=str(tmp_path / "universe.db"),
            bars_db=str(bars_db),
            artifact_dir=str(tmp_path / "artifacts"),
            lookback_bars=1,
            now=as_of,
        )

    assert isinstance(captured["provider"], AlpacaMarketDataProvider)
    assert calls
    assert {call["url"] for call in calls} == {"https://data.alpaca.markets/v2/stocks/bars"}
    assert {call["params"]["feed"] for call in calls} == {"sip"}
    assert {call["params"]["adjustment"] for call in calls} == {"all"}
    assert all(call["params"]["end"].endswith("Z") for call in calls)
    assert all(str(pd.Timestamp(call["params"]["start"]).tz) == "UTC" for call in calls)
    brk_calls = [call for call in calls if call["params"]["symbols"] == "BRK.B"]
    assert {call["params"]["timeframe"] for call in brk_calls} >= {"1Day", "1Hour"}
    daily_calls = [call for call in brk_calls if call["params"]["timeframe"] == "1Day"]
    assert daily_calls[0]["params"].get("page_token") is None
    assert daily_calls[1]["params"]["page_token"] == "page-2"
    assert all(call["headers"]["APCA-API-KEY-ID"] == "key-id" for call in calls)
    assert all(call["headers"]["APCA-API-SECRET-KEY"] == "key-secret" for call in calls)
    logged = caplog.text
    assert "key-id" not in logged
    assert "key-secret" not in logged
    for call in calls:
        assert "key-secret" not in call["url"]
        assert "key-secret" not in str(call["params"])

    store = BarStore(str(bars_db))
    stored_daily = store.load_window("BRK-B", "1d", "2000-01-01", "2100-01-01")
    stored_hourly = store.load_window("BRK-B", "1h", "2000-01-01", "2100-01-01")
    assert len(stored_daily) == len(daily)
    assert len(stored_hourly) == len(hourly)
    assert store.load_window("ZZZ", "1d", "2000-01-01", "2100-01-01").empty
    assert store.load_window("ZZZ", "1h", "2000-01-01", "2100-01-01").empty
    assert rows[0]["symbol"] == "BRK-B"

    before = len(calls)
    minute = captured["provider"].get_bars("BRK-B", "2024-06-03T14:00:00Z", "2024-06-03T15:00:00Z", "1m")
    minute_call = calls[before]
    assert minute_call["params"]["timeframe"] == "1Min"
    assert minute_call["params"]["symbols"] == "BRK.B"
    assert minute_call["params"]["feed"] == "sip"
    assert minute_call["params"]["adjustment"] == "all"
    assert list(minute.index) == [pd.Timestamp("2024-06-03T14:30:00Z")]
    assert minute["close"].iloc[0] == 10.5


def _scan_paths(tmp_path):
    return {
        "db_path": str(tmp_path / "universe.db"),
        "bars_db": str(tmp_path / "market_bars.db"),
        "artifact_dir": str(tmp_path / "artifacts"),
        "lookback_bars": 1,
    }


def test_missing_alpaca_credentials_stop_before_any_snapshot(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("credentials must be checked before a request")

    monkeypatch.setattr("src.data.provider.requests.Session", Session)
    paths = _scan_paths(tmp_path)
    with pytest.raises(MarketDataUnavailable, match="ALPACA_API_KEY"):
        run_universe(["AAPL", "MSFT"], now=pd.Timestamp("2026-03-09T15:00:00Z"), **paths)
    assert calls == []
    assert count_snapshots(paths["db_path"]) == 0
    assert not (tmp_path / "artifacts").exists()


def test_alpaca_auth_rejection_stops_on_the_first_request(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "key-secret")
    calls = []

    class Response:
        status_code = 401

        def raise_for_status(self):
            raise AssertionError("auth rejection must not become a per-symbol HTTP error")

        def json(self):
            return {"message": "key-secret"}

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "headers": dict(headers or {})})
            return Response()

    monkeypatch.setattr("src.data.provider.requests.Session", Session)
    paths = _scan_paths(tmp_path)
    with pytest.raises(MarketDataUnavailable, match=r"rejected the request \(401\)") as caught:
        run_universe(["AAPL", "MSFT"], now=pd.Timestamp("2026-03-09T15:00:00Z"), **paths)
    assert "key-id" not in str(caught.value)
    assert "key-secret" not in str(caught.value)
    assert len(calls) == 1
    assert count_snapshots(paths["db_path"]) == 0


def test_forbidden_sip_feed_stops_before_any_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "key-secret")

    class Response:
        status_code = 403

        def raise_for_status(self):
            raise AssertionError("forbidden feed must stop the scan")

        def json(self):
            return {}

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            return Response()

    monkeypatch.setattr("src.data.provider.requests.Session", Session)
    paths = _scan_paths(tmp_path)
    with pytest.raises(MarketDataUnavailable, match=r"rejected the request \(403\)"):
        run_universe(["BRK-B"], now=pd.Timestamp("2026-03-09T15:00:00Z"), **paths)
    assert count_snapshots(paths["db_path"]) == 0


def test_one_symbol_failure_still_persists_the_symbols_that_loaded(tmp_path):
    daily, hourly, as_of = _frames("up")

    class MixedProvider:
        def get_bars(self, symbol, start, end, interval):
            if symbol == "BAD":
                raise RuntimeError("symbol feed failed")
            frame = daily if interval == "1d" else hourly
            return frame

    paths = _scan_paths(tmp_path)
    rows = run_universe(["GOOD", "BAD"], provider=MixedProvider(), now=as_of, **paths)
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["GOOD"]["final_score"] is not None
    assert by_symbol["BAD"]["veto_status"] == "BLOCKED_INSUFFICIENT_DATA"
    assert count_snapshots(paths["db_path"]) == 2


def test_every_symbol_failing_does_not_write_an_insufficient_snapshot(tmp_path):
    class DownProvider:
        def get_bars(self, symbol, start, end, interval):
            raise RuntimeError(f"{symbol} feed failed")

    paths = _scan_paths(tmp_path)
    with pytest.raises(MarketDataUnavailable, match="every symbol"):
        run_universe(
            ["AAPL", "MSFT"],
            provider=DownProvider(),
            now=pd.Timestamp("2026-03-09T15:00:00Z"),
            **paths,
        )
    assert count_snapshots(paths["db_path"]) == 0
    assert not (tmp_path / "artifacts").exists()


def test_current_snapshot_uses_the_scan_clock_and_history_stays_hourly(tmp_path):
    daily, hourly, as_of = _frames("up")
    decision = as_of + pd.Timedelta(minutes=2)
    provider = MapProvider({"AAPL": daily}, {"AAPL": hourly})
    paths = _scan_paths(tmp_path)
    paths["lookback_bars"] = 3
    rows = run_universe(["AAPL"], provider=provider, now=decision, **paths)
    history = load_symbol_history(paths["db_path"], "AAPL", limit=10)
    hourly_stamps = [to_utc_iso(stamp) for stamp in list(hourly.index[-3:])]
    assert [row["scan_timestamp"] for row in history[:-1]] == hourly_stamps
    assert history[-1]["scan_timestamp"] == to_utc_iso(decision)
    assert rows[0]["scan_timestamp"] == to_utc_iso(decision)
    assert rows[0]["hourly_close"] is not None
    stored = load_latest_scan(paths["db_path"])
    assert stored[0]["executed_at"] != stored[0]["scan_timestamp"]
    assert stored[0]["scan_timestamp"] == to_utc_iso(decision)


def test_a_single_symbol_failure_still_writes_its_snapshot(tmp_path):
    class DownProvider:
        def get_bars(self, symbol, start, end, interval):
            raise RuntimeError("symbol feed failed")

    paths = _scan_paths(tmp_path)
    rows = run_universe(["AAPL"], provider=DownProvider(), now=pd.Timestamp("2026-03-09T15:00:00Z"), **paths)
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["veto_status"] == "BLOCKED_INSUFFICIENT_DATA"
    assert count_snapshots(paths["db_path"]) == 1

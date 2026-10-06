import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src.data.provider import AlpacaMarketDataProvider


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        token = (params or {}).get("page_token")
        return _Response(self.pages[token])


def _bar(stamp, close, volume=100):
    return {"t": stamp, "o": close, "h": close + 1, "l": close - 1, "c": close, "v": volume}


def _provider(monkeypatch, pages):
    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "key-secret")
    session = _Session(pages)
    return AlpacaMarketDataProvider(session=session), session


def test_missing_credentials_raise_before_any_request(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    session = _Session({})
    provider = AlpacaMarketDataProvider(session=session)
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
        provider.get_bars("AAPL", "2024-01-01", "2024-02-01", "1d")
    assert session.calls == []

    monkeypatch.setenv("ALPACA_API_KEY", "key-id")
    provider = AlpacaMarketDataProvider(session=session)
    with pytest.raises(RuntimeError, match="ALPACA_API_SECRET"):
        provider.get_bars("AAPL", "2024-01-01", "2024-02-01", "1d")
    assert session.calls == []


def test_auth_headers_feed_and_pagination(monkeypatch):
    pages = {
        None: {
            "bars": {"AAPL": [_bar("2024-01-02T05:00:00Z", 10, 1000)]},
            "next_page_token": "page-2",
        },
        "page-2": {
            "bars": {"AAPL": [_bar("2024-01-03T05:00:00Z", 11, 1100)]},
            "next_page_token": None,
        },
    }
    provider, session = _provider(monkeypatch, pages)
    frame = provider.get_bars("AAPL", "2024-01-01", "2024-01-04", "1d")

    assert [call["url"] for call in session.calls] == ["https://data.alpaca.markets/v2/stocks/bars"] * 2
    assert session.calls[0]["headers"]["APCA-API-KEY-ID"] == "key-id"
    assert session.calls[0]["headers"]["APCA-API-SECRET-KEY"] == "key-secret"
    assert session.calls[0]["params"]["feed"] == "sip"
    assert session.calls[0]["params"]["adjustment"] == "all"
    assert session.calls[0]["params"]["timeframe"] == "1Day"
    assert session.calls[0]["params"]["symbols"] == "AAPL"
    assert "page_token" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["page_token"] == "page-2"
    assert session.calls[1]["params"]["feed"] == "sip"
    assert list(frame["close"]) == [10, 11]
    assert list(frame["volume"]) == [1000, 1100]
    assert str(frame.index.tz) == "UTC"


def test_symbol_mapping_and_intervals(monkeypatch):
    pages = {
        None: {"bars": {"BRK.B": [_bar("2024-06-03T14:00:00Z", 400, 50)]}, "next_page_token": None},
    }
    provider, session = _provider(monkeypatch, pages)
    hourly = provider.get_bars("BRK-B", "2024-06-03T13:00:00Z", "2024-06-03T16:00:00Z", "1h")
    assert session.calls[0]["params"]["symbols"] == "BRK.B"
    assert session.calls[0]["params"]["timeframe"] == "1Hour"
    assert hourly.index[0] == pd.Timestamp("2024-06-03T14:00:00Z")

    pages[None] = {"bars": {"BF.B": [_bar("2024-06-03T04:00:00Z", 70, 80)]}, "next_page_token": None}
    session.calls.clear()
    daily = provider.get_bars("bf-b", "2024-06-01", "2024-06-04", "1d")
    assert session.calls[0]["params"]["symbols"] == "BF.B"
    assert session.calls[0]["params"]["timeframe"] == "1Day"
    assert daily["close"].iloc[0] == 70


def test_inclusive_alpaca_end_is_clipped_to_exclusive_contract(monkeypatch):
    pages = {
        None: {
            "bars": {
                "AAPL": [
                    _bar("2024-01-01T00:00:00Z", 1),
                    _bar("2024-01-02T14:00:00Z", 2),
                    _bar("2024-01-03T00:00:00Z", 3),
                ]
            },
            "next_page_token": None,
        }
    }
    provider, _session = _provider(monkeypatch, pages)
    frame = provider.get_bars("AAPL", "2024-01-02T14:00:00Z", "2024-01-03T00:00:00Z", "1h")
    assert list(frame.index) == [pd.Timestamp("2024-01-02T14:00:00Z")]
    assert frame["close"].iloc[0] == 2


def test_empty_and_missing_symbol_return_an_empty_ohlcv_frame(monkeypatch):
    pages = {None: {"bars": None, "next_page_token": None}}
    provider, _session = _provider(monkeypatch, pages)
    empty = provider.get_bars("AAPL", "2024-01-01", "2024-02-01", "1d")
    assert list(empty.columns) == ["open", "high", "low", "close", "volume"]
    assert empty.empty

    pages[None] = {"bars": {}, "next_page_token": None}
    missing = provider.get_bars("AAPL", "2024-01-01", "2024-02-01", "1h")
    assert missing.empty
    assert list(missing.columns) == ["open", "high", "low", "close", "volume"]

    pages[None] = {"bars": {"MSFT": [_bar("2024-01-02T15:00:00Z", 9)]}, "next_page_token": None}
    other = provider.get_bars("AAPL", "2024-01-01", "2024-02-01", "1h")
    assert other.empty


def _alignment_module():
    path = ROOT / "scripts" / "ab_alpaca_yahoo.py"
    spec = importlib.util.spec_from_file_location("ab_alpaca_yahoo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alignment_union_does_not_invent_bars_and_splits_session():
    module = _alignment_module()
    yahoo = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10.0},
        index=pd.to_datetime(["2024-01-02T15:00:00Z", "2024-01-02T21:00:00Z"], utc=True),
    )
    alpaca = pd.DataFrame(
        {"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1, "volume": 12.0},
        index=pd.to_datetime(["2024-01-02T15:00:00Z", "2024-01-02T16:00:00Z"], utc=True),
    )
    rows = module.align_frames("AAPL", "1h", yahoo, alpaca)
    by_stamp = {row["timestamp"]: row for row in rows}
    assert set(by_stamp) == {
        "2024-01-02T15:00:00+00:00",
        "2024-01-02T16:00:00+00:00",
        "2024-01-02T21:00:00+00:00",
    }
    assert by_stamp["2024-01-02T15:00:00+00:00"]["presence"] == "BOTH_PRESENT"
    assert by_stamp["2024-01-02T15:00:00+00:00"]["in_expected_session"] is True
    # 11:00 ET is inside the regular session; only Alpaca printed it.
    alpaca_only = by_stamp["2024-01-02T16:00:00+00:00"]
    assert alpaca_only["presence"] == "ALPACA_ONLY"
    assert alpaca_only["in_expected_session"] is True
    assert alpaca_only["yahoo_prev"] == "2024-01-02T15:00:00+00:00"
    assert alpaca_only["yahoo_next"] == "2024-01-02T21:00:00+00:00"
    # 16:00 ET is the session close label, outside the [09:30, 16:00) window.
    outside = by_stamp["2024-01-02T21:00:00+00:00"]
    assert outside["presence"] == "YAHOO_ONLY"
    assert outside["in_expected_session"] is False
    assert "2024-01-02T17:00:00+00:00" not in by_stamp


def test_request_end_stays_timezone_aware_and_is_not_parsed_as_a_date():
    from datetime import datetime

    import yfinance.utils as yf_utils

    module = _alignment_module()
    now = pd.Timestamp("2026-10-04T18:15:00+00:00")
    end = module.request_end(now)
    assert end == pd.Timestamp("2026-10-04T17:55:00+00:00")
    assert str(end.tz) == "UTC"
    with pytest.raises(ValueError, match="unconverted data remains"):
        datetime.strptime(end.isoformat(), "%Y-%m-%d")
    parsed = yf_utils._parse_user_dt(end, "America/New_York")
    assert parsed == end.tz_convert("America/New_York")


def test_timezone_aware_end_keeps_the_exclusive_bound(monkeypatch):
    pages = {
        None: {
            "bars": {
                "AAPL": [
                    _bar("2026-10-04T13:30:00Z", 1),
                    _bar("2026-10-04T14:30:00Z", 2),
                    _bar("2026-10-04T17:55:00Z", 3),
                ]
            },
            "next_page_token": None,
        }
    }
    provider, session = _provider(monkeypatch, pages)
    end = pd.Timestamp("2026-10-04T17:55:00+00:00")
    frame = provider.get_bars("AAPL", "2026-10-01", end, "1h")
    assert session.calls[0]["params"]["end"] == "2026-10-04T17:55:00Z"
    assert session.calls[0]["params"]["feed"] == "sip"
    assert list(frame["close"]) == [1, 2]


def test_one_minute_requests_use_the_sip_minute_timeframe(monkeypatch):
    pages = {
        None: {
            "bars": {"AAPL": [_bar("2026-03-09T14:00:00Z", 10, 5), _bar("2026-03-09T14:01:00Z", 11, 6)]},
            "next_page_token": None,
        }
    }
    provider, session = _provider(monkeypatch, pages)
    frame = provider.get_bars("AAPL", "2026-03-09T14:00:00Z", "2026-03-09T15:00:00Z", "1m")
    assert session.calls[0]["params"]["timeframe"] == "1Min"
    assert session.calls[0]["params"]["feed"] == "sip"
    assert session.calls[0]["params"]["adjustment"] == "all"
    assert list(frame.index) == [
        pd.Timestamp("2026-03-09T14:00:00Z"),
        pd.Timestamp("2026-03-09T14:01:00Z"),
    ]
    assert list(frame["volume"]) == [5, 6]


def test_sip_request_end_stops_fifteen_minutes_before_now(monkeypatch):
    fixed_now = pd.Timestamp("2026-10-06T17:02:00Z")
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda tz=None: fixed_now))
    pages = {None: {"bars": {"AAPL": [_bar("2026-10-06T16:00:00Z", 10)]}, "next_page_token": None}}
    provider, session = _provider(monkeypatch, pages)
    provider.get_bars("AAPL", "2026-10-01", "2026-10-07", "1d")
    assert session.calls[0]["params"]["end"] == "2026-10-06T16:47:00Z"
    assert session.calls[0]["params"]["feed"] == "sip"

    session.calls.clear()
    empty = provider.get_bars("AAPL", "2026-10-06T17:00:00Z", "2026-10-06T17:02:00Z", "1m")
    assert session.calls == []
    assert empty.empty


def test_daily_weekday_is_in_session_and_weekend_is_not():
    module = _alignment_module()
    weekday = pd.Timestamp("2024-01-02T05:00:00Z")
    weekend = pd.Timestamp("2024-01-06T05:00:00Z")
    assert module.in_expected_session(weekday, "1d") is True
    assert module.in_expected_session(weekend, "1d") is False
    assert module.in_expected_session(pd.Timestamp("2024-01-06T15:00:00Z"), "1h") is False

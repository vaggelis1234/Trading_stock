"""Market-data seam for Universe v1.

Metric, veto, scoring, and ranking code depend only on OHLCV frames.
``AlpacaMarketDataProvider`` is the production provider. Universe scans
construct it when no provider is passed. ``YFinanceMarketDataProvider``
remains for tests and the Yahoo comparison script.
"""

import os
from typing import Optional, Protocol

import pandas as pd
import requests

from src.data.ingest import fetch_historical_yfinance

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_ALPACA_AUTH_STATUSES = frozenset({401, 403})


class MarketDataUnavailable(RuntimeError):
    """The production market-data source cannot serve this scan.

    Raised for missing Alpaca credentials and for an auth rejection from the
    SIP endpoint. A later per-symbol fetch error is not this failure.
    """


class MarketDataProvider(Protocol):
    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        """Return OHLCV bars for ``interval`` (``1d`` or ``1h``).

        ``AlpacaMarketDataProvider`` also accepts ``1m``, requested from SIP
        as the ``1Min`` timeframe. That frame is the minute lookup for hourly
        coverage. It is not a scoring timeframe.
        """


def prepare_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize a provider frame to UTC OHLCV.

    Naive timestamps are interpreted as UTC. Rows are sorted, floored to the
    minute, and de-duplicated so hourly scans align across symbols.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    out = df.copy()
    out.columns = [str(column).lower() for column in out.columns]
    missing = [column for column in OHLCV_COLUMNS if column not in out.columns]
    if missing:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    out = out.loc[:, OHLCV_COLUMNS]
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index, utc=True)).floor("min")
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out = out.dropna(subset=["close"])
    return out


class YFinanceMarketDataProvider:
    """Historical OHLCV via the existing yfinance ingest helper."""

    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        raw = fetch_historical_yfinance(symbol, start=start, end=end, interval=interval)
        return prepare_ohlcv(raw)


_ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_ALPACA_SYMBOLS = {"BRK-B": "BRK.B", "BF-B": "BF.B"}
_ALPACA_TIMEFRAMES = {"1d": "1Day", "1h": "1Hour", "1m": "1Min"}
_ALPACA_PAGE_LIMIT = 10000
_ALPACA_MAX_PAGES = 500
_SIP_RECENT_DELAY = pd.Timedelta(minutes=15)


def _bound(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _rfc3339(stamp: pd.Timestamp) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def _sip_request_end(requested: pd.Timestamp) -> pd.Timestamp:
    """Keep a SIP query out of the trailing 15 minutes.

    This subscription rejects the whole request when ``end`` reaches recent
    SIP data. Older bars in the same window stay available.
    """
    latest = pd.Timestamp.now(tz="UTC") - _SIP_RECENT_DELAY
    if requested > latest:
        return latest
    return requested


class AlpacaMarketDataProvider:
    """Production historical SIP bars for Universe scans.

    Requests ``feed=sip`` only. Prices use ``adjustment=all``. Credentials
    are read from ``ALPACA_API_KEY`` and ``ALPACA_API_SECRET``.
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self._api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        self._api_secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        self._session = session or requests.Session()

    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        self._require_credentials()
        timeframe = _ALPACA_TIMEFRAMES.get(interval)
        if timeframe is None:
            raise ValueError(f"unsupported interval {interval!r}")
        start_ts = _bound(start)
        end_ts = _bound(end)
        request_end = _sip_request_end(end_ts)
        if start_ts >= request_end:
            return prepare_ohlcv(None)
        alpaca_symbol = _ALPACA_SYMBOLS.get(str(symbol).strip().upper(), str(symbol).strip().upper())
        raw = self._pages(alpaca_symbol, timeframe, start_ts, request_end)
        frame = prepare_ohlcv(raw)
        if frame.empty:
            return frame
        clipped = frame.loc[(frame.index >= start_ts) & (frame.index < end_ts)]
        if clipped.empty:
            return prepare_ohlcv(None)
        return clipped

    def _require_credentials(self) -> None:
        if not self._api_key or not self._api_secret:
            raise MarketDataUnavailable(
                "ALPACA_API_KEY and ALPACA_API_SECRET are required for historical SIP bars"
            )

    def _pages(self, symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "feed": "sip",
            "adjustment": "all",
            "limit": _ALPACA_PAGE_LIMIT,
            "sort": "asc",
        }
        rows = []
        seen_tokens = set()
        token = None
        for _ in range(_ALPACA_MAX_PAGES):
            query = dict(params)
            if token:
                query["page_token"] = token
            payload = self._get(query)
            bars = payload.get("bars") or {}
            rows.extend(bars.get(symbol) or [])
            token = payload.get("next_page_token") or None
            if not token or token in seen_tokens:
                break
            seen_tokens.add(token)
        return _bars_frame(rows)

    def _get(self, params: dict) -> dict:
        response = self._session.get(
            _ALPACA_BARS_URL,
            params=params,
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._api_secret,
            },
            timeout=60,
        )
        status = getattr(response, "status_code", None)
        if status in _ALPACA_AUTH_STATUSES:
            raise MarketDataUnavailable(f"Alpaca market data rejected the request ({status})")
        response.raise_for_status()
        return response.json()


def _bars_frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    frame = pd.DataFrame(
        {
            "open": [row.get("o") for row in rows],
            "high": [row.get("h") for row in rows],
            "low": [row.get("l") for row in rows],
            "close": [row.get("c") for row in rows],
            "volume": [row.get("v") for row in rows],
        },
        index=pd.to_datetime([row.get("t") for row in rows], utc=True),
    )
    frame.index.name = None
    return frame

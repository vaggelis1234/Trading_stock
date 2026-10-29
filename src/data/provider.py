"""Market-data seam for Universe v1.

Metric, veto, scoring, and ranking code depend only on OHLCV frames.
``AlpacaMarketDataProvider`` is the production provider. Universe scans
construct it when no provider is passed. ``YFinanceMarketDataProvider``
remains for tests and the Yahoo comparison script.
"""

import logging
import math
import os
import time
from typing import Optional, Protocol

import pandas as pd
import requests

from src.data.ingest import fetch_historical_yfinance

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_ALPACA_AUTH_STATUSES = frozenset({401, 403})
# Extra attempts after the first HTTP 429. Five responses, then a clean failure.
ALPACA_429_MAX_RETRIES = 4
# Used only when the 429 has no numeric Retry-After: 0.5, 1, 2, 4, then the cap.
ALPACA_BACKOFF_BASE_SECONDS = 0.5
ALPACA_BACKOFF_MAX_SECONDS = 8.0
# Pause before each request after the first. Stays under Alpaca's 200/minute pace.
ALPACA_REQUEST_DELAY_SECONDS = 0.3
_ALPACA_REQUEST_DELAY_ENV = "ALPACA_REQUEST_DELAY"


class MarketDataUnavailable(RuntimeError):
    """The production market-data source cannot serve this scan.

    Raised for missing Alpaca credentials and for an auth rejection from the
    SIP endpoint. A later per-symbol fetch error is not this failure.
    """


class MarketDataProvider(Protocol):
    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        """Return OHLCV bars for ``interval`` (``1d`` or ``1h``).

        ``AlpacaMarketDataProvider`` also accepts ``1m`` (SIP ``1Min``, the
        minute lookup for hourly coverage, not a scoring timeframe) and
        ``30m`` (SIP ``30Min``, used to reconstruct a completed session
        window). Historical requests stay on ``feed=sip`` and keep the
        15-minute delay. That feed is not a real-time subscription.
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
_ALPACA_TIMEFRAMES = {"1d": "1Day", "1h": "1Hour", "30m": "30Min", "1m": "1Min"}
_ALPACA_PAGE_LIMIT = 10000
_ALPACA_MAX_PAGES = 500
# The bars endpoint does not publish a symbol cap. Stay under a short query
# and under the 10_000-bar page, whichever is smaller for the window.
ALPACA_SYMBOL_BATCH_CAP = 50
# Fifteen minutes is still inside the window this subscription rejects
# ("subscription does not permit querying recent SIP data"). Sixteen clears it.
_SIP_RECENT_DELAY = pd.Timedelta(minutes=16)
_ALPACA_FROM_WIRE = {remote: local for local, remote in _ALPACA_SYMBOLS.items()}


def _bound(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _rfc3339(stamp: pd.Timestamp) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def _to_alpaca_symbol(symbol: str) -> str:
    text = str(symbol).strip().upper()
    return _ALPACA_SYMBOLS.get(text, text)


def _from_alpaca_symbol(symbol: str) -> str:
    return _ALPACA_FROM_WIRE.get(symbol, symbol)


def batch_size_for_window(interval: str, start: str, end: str) -> int:
    """How many symbols share one request without exceeding one full page.

    ``1d`` counts calendar days in the requested window. ``1h`` counts hours.
    ``30m`` is one cash session of at most 13 bars, so the batch stays at
    ``ALPACA_SYMBOL_BATCH_CAP`` instead of shrinking with the calendar span.
    The page limit is 10_000 bars across every symbol.
    """
    if interval == "30m":
        return ALPACA_SYMBOL_BATCH_CAP
    start_ts = _bound(start)
    end_ts = _bound(end)
    span_seconds = max(0.0, (end_ts - start_ts).total_seconds())
    if interval == "1d":
        max_bars = max(1, math.ceil(span_seconds / 86400))
    elif interval == "1h":
        max_bars = max(1, math.ceil(span_seconds / 3600))
    elif interval == "1m":
        max_bars = max(1, math.ceil(span_seconds / 60))
    else:
        max_bars = _ALPACA_PAGE_LIMIT
    return max(1, min(ALPACA_SYMBOL_BATCH_CAP, _ALPACA_PAGE_LIMIT // max_bars))


class _BarsRequestError(Exception):
    """A bars request that did not finish. ``kind`` is ``bad_request`` or ``failed``."""

    def __init__(self, kind: str, status: Optional[int] = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


class BarsBatch:
    """Completed frames and symbols that must not be written.

    A symbol in ``frames`` finished pagination. An empty frame is a confirmed
    empty series. A symbol in ``failed`` was not confirmed and must keep its
    previous bars and coverage.
    """

    def __init__(self, frames: dict[str, pd.DataFrame], failed: tuple[str, ...] = ()):
        self.frames = frames
        self.failed = tuple(failed)


class _Attempt:
    def __init__(self, kind: str, batch: Optional[BarsBatch] = None, status: Optional[int] = None):
        self.kind = kind
        self.batch = batch
        self.status = status


def _sip_request_end(requested: pd.Timestamp) -> pd.Timestamp:
    """Keep a SIP query out of the trailing 16 minutes.

    This subscription rejects the whole request when ``end`` reaches recent
    SIP data, including an end exactly 15 minutes ago. Older bars in the
    same window stay available. Scan timing still waits 15 minutes after
    a window ends; the extra minute is only the query bound.
    """
    latest = pd.Timestamp.now(tz="UTC") - _SIP_RECENT_DELAY
    if requested > latest:
        return latest
    return requested


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _request_delay_seconds(explicit: Optional[float]) -> float:
    """Inter-request pause. An explicit value wins, then the env var, then the default."""
    if explicit is not None:
        delay = float(explicit)
    else:
        raw = os.environ.get(_ALPACA_REQUEST_DELAY_ENV, "").strip()
        delay = float(raw) if raw else ALPACA_REQUEST_DELAY_SECONDS
    if delay < 0:
        raise ValueError(f"{_ALPACA_REQUEST_DELAY_ENV} must be >= 0")
    return delay


def _retry_after_seconds(response) -> Optional[float]:
    """Numeric ``Retry-After`` in seconds, or ``None`` when the header is absent or not a delay."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return None
    return seconds


def _rate_limit_delay(response, attempt: int) -> float:
    """Wait from ``Retry-After`` when it is a number of seconds, otherwise capped exponential backoff."""
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        return retry_after
    growth = ALPACA_BACKOFF_BASE_SECONDS * (2 ** attempt)
    return min(growth, ALPACA_BACKOFF_MAX_SECONDS)


class AlpacaMarketDataProvider:
    """Production historical SIP bars for Universe scans.

    Requests ``feed=sip`` only. Prices use ``adjustment=all``. Credentials
    are read from ``ALPACA_API_KEY`` and ``ALPACA_API_SECRET``.

    HTTP 429 is retried at most ``ALPACA_429_MAX_RETRIES`` times. A numeric
    ``Retry-After`` is the wait; otherwise the wait doubles from
    ``ALPACA_BACKOFF_BASE_SECONDS`` and stops growing at
    ``ALPACA_BACKOFF_MAX_SECONDS``. Other HTTP statuses are not retried.
    """

    def __init__(self, session: Optional[requests.Session] = None, request_delay: Optional[float] = None):
        self._api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        self._api_secret = os.environ.get("ALPACA_API_SECRET", "").strip()
        self._session = session or requests.Session()
        self._request_delay = _request_delay_seconds(request_delay)
        self._paced = False

    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        self._require_credentials()
        timeframe = _ALPACA_TIMEFRAMES.get(interval)
        if timeframe is None:
            raise ValueError(f"unsupported interval {interval!r}")
        start_ts = _bound(start)
        end_ts = _bound(end)
        symbol_text = str(symbol).strip()
        if interval == "30m":
            batch = self.get_bars_many([symbol_text], start, end, interval)
            if symbol_text in batch.failed or symbol_text not in batch.frames:
                raise requests.HTTPError("HTTP 400")
            return batch.frames[symbol_text]
        outcome = self._attempt([symbol_text], timeframe, start_ts, end_ts)
        if outcome.kind != "ok" or outcome.batch is None:
            raise requests.HTTPError(f"HTTP {outcome.status}")
        return outcome.batch.frames[symbol_text]

    def get_bars_many(self, symbols: list[str], start: str, end: str, interval: str) -> BarsBatch:
        """Fetch one shared window for many symbols.

        Pagination must finish before any frame is returned. A first-page HTTP
        400 splits the batch in half. If both halves also return 400, every
        symbol in that batch is failed and no further split is attempted.
        """
        self._require_credentials()
        timeframe = _ALPACA_TIMEFRAMES.get(interval)
        if timeframe is None:
            raise ValueError(f"unsupported interval {interval!r}")
        start_ts = _bound(start)
        end_ts = _bound(end)
        ordered = list(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
        if not ordered:
            return BarsBatch({})
        windows = _cash_session_windows(start_ts, end_ts) if interval == "30m" else [(start_ts, end_ts)]
        if interval == "30m" and not windows:
            return BarsBatch({symbol: prepare_ohlcv(None) for symbol in ordered})
        pieces: dict[str, list[pd.DataFrame]] = {}
        failed: set[str] = set()
        for window_start, window_end in windows:
            size = batch_size_for_window(interval, window_start.isoformat(), window_end.isoformat())
            for offset in range(0, len(ordered), size):
                chunk = ordered[offset:offset + size]
                finished = self._finish(
                    chunk,
                    self._attempt(chunk, timeframe, window_start, window_end),
                    timeframe,
                    window_start,
                    window_end,
                )
                failed.update(finished.failed)
                for symbol, frame in finished.frames.items():
                    if symbol in failed or frame is None or frame.empty:
                        continue
                    pieces.setdefault(symbol, []).append(frame)
        frames: dict[str, pd.DataFrame] = {}
        for symbol in ordered:
            if symbol in failed:
                continue
            parts = pieces.get(symbol, [])
            if not parts:
                frames[symbol] = prepare_ohlcv(None)
                continue
            merged = pd.concat(parts)
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            frames[symbol] = merged
        return BarsBatch(frames, tuple(symbol for symbol in ordered if symbol in failed))

    def _require_credentials(self) -> None:
        if not self._api_key or not self._api_secret:
            raise MarketDataUnavailable(
                "ALPACA_API_KEY and ALPACA_API_SECRET are required for historical SIP bars"
            )

    def _attempt(self, symbols: list[str], timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> _Attempt:
        try:
            rows = self._load_rows(symbols, timeframe, start, end)
        except _BarsRequestError as exc:
            return _Attempt(exc.kind, status=exc.status)
        frames = {symbol: _clip_bars(rows.get(symbol, []), start, end) for symbol in symbols}
        return _Attempt("ok", BarsBatch(frames, ()))

    def _finish(
        self,
        symbols: list[str],
        outcome: _Attempt,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> BarsBatch:
        if outcome.kind == "ok" and outcome.batch is not None:
            return outcome.batch
        if outcome.kind != "bad_request" or len(symbols) <= 1:
            return BarsBatch({}, tuple(symbols))
        mid = len(symbols) // 2
        left_symbols = symbols[:mid]
        right_symbols = symbols[mid:]
        left = self._attempt(left_symbols, timeframe, start, end)
        right = self._attempt(right_symbols, timeframe, start, end)
        if left.kind == "bad_request" and right.kind == "bad_request":
            logger.warning(
                "Alpaca HTTP 400 for %s symbols; both halves failed so the batch was not split further",
                len(symbols),
            )
            return BarsBatch({}, tuple(symbols))
        left_done = self._finish(left_symbols, left, timeframe, start, end)
        right_done = self._finish(right_symbols, right, timeframe, start, end)
        frames = dict(left_done.frames)
        frames.update(right_done.frames)
        return BarsBatch(frames, left_done.failed + right_done.failed)

    def _load_rows(self, symbols: list[str], timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, list]:
        request_end = _sip_request_end(end)
        if timeframe == "30Min" and request_end >= end:
            # Alpaca treats ``end`` as inclusive. A 30-minute bar is stamped at
            # its start, so the exclusive session close must not be requested.
            request_end = end - pd.Timedelta(microseconds=1)
        collected = {symbol: [] for symbol in symbols}
        if start >= request_end:
            return collected
        remote_for = {symbol: _to_alpaca_symbol(symbol) for symbol in symbols}
        local_for: dict[str, str] = {}
        for symbol, remote in remote_for.items():
            local_for.setdefault(remote, symbol)
        params = {
            "symbols": ",".join(dict.fromkeys(remote_for[symbol] for symbol in symbols)),
            "timeframe": timeframe,
            "start": _rfc3339(start),
            "end": _rfc3339(request_end),
            "feed": "sip",
            "adjustment": "all",
            "limit": _ALPACA_PAGE_LIMIT,
            "sort": "asc",
        }
        seen_tokens: set[str] = set()
        token = None
        for page_index in range(_ALPACA_MAX_PAGES):
            query = dict(params)
            if token:
                query["page_token"] = token
            try:
                payload = self._get(query)
            except requests.HTTPError as exc:
                status = _http_status(exc)
                if status == 400 and page_index == 0:
                    raise _BarsRequestError("bad_request", 400) from exc
                raise _BarsRequestError("failed", status) from exc
            bars = payload.get("bars") or {}
            if not isinstance(bars, dict):
                raise _BarsRequestError("failed")
            for remote, rows in bars.items():
                local = local_for.get(remote)
                if local is None:
                    local = _from_alpaca_symbol(str(remote))
                    if local not in collected:
                        continue
                if rows:
                    collected[local].extend(rows)
            token = payload.get("next_page_token") or None
            if not token:
                return collected
            if token in seen_tokens:
                raise _BarsRequestError("failed")
            seen_tokens.add(token)
        raise _BarsRequestError("failed")

    def _pace(self) -> None:
        if self._paced and self._request_delay > 0:
            _sleep(self._request_delay)
        self._paced = True

    def _get(self, params: dict) -> dict:
        self._pace()
        for attempt in range(ALPACA_429_MAX_RETRIES + 1):
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
            if status == 429 and attempt < ALPACA_429_MAX_RETRIES:
                delay = _rate_limit_delay(response, attempt)
                logger.warning(
                    "Alpaca HTTP 429; waiting %.2fs before retry %s/%s",
                    delay,
                    attempt + 1,
                    ALPACA_429_MAX_RETRIES,
                )
                _sleep(delay)
                continue
            response.raise_for_status()
            if status == 429:
                raise requests.HTTPError(
                    "Alpaca market data rate limit persisted after retries (429)",
                    response=response,
                )
            return response.json()
        raise requests.HTTPError("Alpaca market data rate limit persisted after retries (429)")


def _http_status(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _cash_session_windows(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Half-open cash sessions that overlap ``[start, end)``.

    Alpaca has no regular-hours filter. A 30-minute request uses one of these
    windows so premarket and after-close bars are never asked for. Weekends
    and holidays are skipped. An early close ends at 13:00 ET.
    """
    from src.market.calendar import NyseCalendar

    from datetime import timedelta

    calendar = NyseCalendar()
    start_et = start.tz_convert("America/New_York")
    end_et = end.tz_convert("America/New_York")
    day = start_et.date()
    last = end_et.date()
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    while day <= last:
        session = calendar.session(day)
        if session is not None:
            midnight = pd.Timestamp(day, tz="America/New_York")
            open_at = midnight + pd.Timedelta(hours=9, minutes=30)
            close_at = midnight + pd.Timedelta(hours=session.close_et.hour, minutes=session.close_et.minute)
            lo = max(open_at, start_et)
            hi = min(close_at, end_et)
            if lo < hi:
                windows.append((lo.tz_convert("UTC"), hi.tz_convert("UTC")))
        day += timedelta(days=1)
    return windows


def _clip_bars(rows: list, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Keep the local ``[start, end)`` contract after Alpaca's inclusive ``end``."""
    frame = prepare_ohlcv(_bars_frame(rows))
    if frame.empty:
        return frame
    clipped = frame.loc[(frame.index >= start) & (frame.index < end)]
    if clipped.empty:
        return prepare_ohlcv(None)
    return clipped


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

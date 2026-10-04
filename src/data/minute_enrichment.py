"""Raw 1-minute facts for PASS candidates already chosen by the hourly ranking.

The numbers describe the last hourly bar that is available at the scan.
Minutes at or after the scan, and minutes outside that hour, are ignored.
Nothing here changes a score, a rank, a veto, or an order.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Sequence

import pandas as pd

from src.data.provider import prepare_ohlcv
from src.data.session_bars import classify_hourly_session

logger = logging.getLogger(__name__)

ENRICHMENT_FIELDS = (
    "scan_timestamp",
    "symbol",
    "hour_timestamp",
    "session_type",
    "expected_minutes",
    "observed_minutes",
    "coverage_ratio",
    "first_price",
    "last_price",
    "intrahour_return",
    "intrahour_high",
    "intrahour_low",
    "close_position",
    "hourly_signal",
    "minutes_with_signal",
    "minutes_against_signal",
    "minute_volume",
    "top5_volume_share",
    "last10_volume_share",
    "return_5m",
    "return_10m",
    "mfe",
    "mae",
)


def enrich_hourly_bar(
    symbol: str,
    scan_timestamp,
    hour_timestamp,
    hourly_open,
    hourly_close,
    minute_bars: Optional[pd.DataFrame],
    as_of,
) -> dict:
    """Intraday facts for one hourly bar from a completed 1-minute lookup.

    ``minute_bars is None`` means the lookup did not run. An empty frame means
    it ran and returned no bars. ``as_of`` is an exclusive end: a minute
    timestamp at or after ``as_of`` is not part of this bar yet.
    """
    hour_start = _as_utc(hour_timestamp)
    scan = _as_utc(as_of).floor("min")
    window_end = min(hour_start + pd.Timedelta(hours=1), scan)
    session = classify_hourly_session(hour_start, _empty_minutes(hour_start) if minute_bars is not None else None)
    record = _blank(symbol, scan, hour_start, session)
    if minute_bars is None or window_end <= hour_start:
        if minute_bars is not None and window_end <= hour_start and session.valid:
            record["observed_minutes"] = 0
            record["expected_minutes"] = session.expected_minutes
            record["coverage_ratio"] = 0.0
            record["minutes_with_signal"] = 0
            record["minutes_against_signal"] = 0
            record["minute_volume"] = 0.0
        return record

    window = _causal_minutes(minute_bars, hour_start, window_end)
    if window is None:
        record["observed_minutes"] = None
        record["coverage_ratio"] = None
        return record

    session = classify_hourly_session(hour_start, window)
    record["session_type"] = session.session_type
    record["expected_minutes"] = session.expected_minutes
    record["observed_minutes"] = session.observed_minutes
    record["coverage_ratio"] = session.coverage_ratio
    signal = _signal(hourly_open, hourly_close)
    record["hourly_signal"] = signal
    if len(window) == 0:
        record["minutes_with_signal"] = 0
        record["minutes_against_signal"] = 0
        record["minute_volume"] = 0.0
        return record

    first = float(window["open"].iloc[0])
    last = float(window["close"].iloc[-1])
    high = float(window["high"].max())
    low = float(window["low"].min())
    record["first_price"] = first
    record["last_price"] = last
    record["intrahour_high"] = high
    record["intrahour_low"] = low
    record["intrahour_return"] = _ratio(last - first, first)
    if high != low:
        record["close_position"] = (last - low) / (high - low)

    with_signal, against_signal = _direction_counts(window, signal)
    record["minutes_with_signal"] = with_signal
    record["minutes_against_signal"] = against_signal

    volume = float(window["volume"].sum())
    record["minute_volume"] = volume
    if volume > 0:
        top = window["volume"].nlargest(min(5, len(window))).sum()
        record["top5_volume_share"] = float(top) / volume
        last_start = hour_start + pd.Timedelta(minutes=50)
        tail = window.loc[(window.index >= last_start) & (window.index < hour_start + pd.Timedelta(hours=1))]
        record["last10_volume_share"] = float(tail["volume"].sum()) / volume

    record["return_5m"] = _lag_return(window, 5, hour_start)
    record["return_10m"] = _lag_return(window, 10, hour_start)
    record["mfe"], record["mae"] = _excursion(signal, first, high, low)
    return record


def enrich_candidates(
    provider,
    candidates: Sequence[dict],
    hourly_by_symbol: dict,
    bar_store=None,
) -> list[dict]:
    """Fetch SIP 1m bars only for ``candidates`` and describe their latest hour.

    ``candidates`` is the post-ranking PASS list. Symbols absent from that
    list are not requested.
    """
    records = []
    for row in candidates:
        symbol = str(row.get("symbol", ""))
        scan = row.get("scan_timestamp")
        hourly = hourly_by_symbol.get(symbol)
        bar = _last_bar(hourly, scan)
        if bar is None:
            records.append(_blank(symbol, _as_utc(scan), None, None))
            continue
        hour_ts = _as_utc(bar.name)
        scan_ts = _as_utc(scan).floor("min")
        window_end = min(hour_ts + pd.Timedelta(hours=1), scan_ts)
        minutes = None
        if window_end > hour_ts:
            try:
                minutes = prepare_ohlcv(provider.get_bars(symbol, hour_ts.isoformat(), window_end.isoformat(), "1m"))
            except Exception:
                logger.exception("1m enrichment lookup failed for %s", symbol)
                minutes = None
        else:
            minutes = _empty_minutes(hour_ts)
        record = enrich_hourly_bar(
            symbol,
            scan_ts,
            hour_ts,
            bar.get("open"),
            bar.get("close"),
            minutes,
            scan_ts,
        )
        if bar_store is not None and minutes is not None and record["session_type"] is not None:
            counted = _causal_minutes(minutes, hour_ts, window_end if window_end > hour_ts else hour_ts)
            if counted is not None:
                bar_store.update_hourly_session(symbol, hour_ts, counted)
        records.append(record)
    return records


def save_minute_enrichment(path: str, rows: Sequence[dict]) -> None:
    """Store raw enrichment rows apart from universe score snapshots."""
    if not rows:
        return
    connection = sqlite3.connect(path)
    try:
        columns = ", ".join(f"{name} TEXT" if name in {"scan_timestamp", "symbol", "hour_timestamp", "session_type", "hourly_signal"} else f"{name} REAL" for name in ENRICHMENT_FIELDS)
        # observed counts are integers; REAL still round-trips the values used here.
        connection.execute(
            f"""CREATE TABLE IF NOT EXISTS minute_enrichment (
                {columns},
                PRIMARY KEY (scan_timestamp, symbol)
            )"""
        )
        placeholders = ", ".join("?" for _ in ENRICHMENT_FIELDS)
        connection.executemany(
            f"""INSERT OR REPLACE INTO minute_enrichment ({", ".join(ENRICHMENT_FIELDS)})
                VALUES ({placeholders})""",
            [tuple(_stored(row.get(name)) for name in ENRICHMENT_FIELDS) for row in rows],
        )
        connection.commit()
    finally:
        connection.close()


def _blank(symbol: str, scan, hour_start, session) -> dict:
    record = {name: None for name in ENRICHMENT_FIELDS}
    record["symbol"] = symbol
    record["scan_timestamp"] = None if scan is None else _as_utc(scan).isoformat()
    record["hour_timestamp"] = None if hour_start is None else _as_utc(hour_start).isoformat()
    if session is not None and session.valid:
        record["session_type"] = session.session_type
        record["expected_minutes"] = session.expected_minutes
    return record


def _empty_minutes(hour_start) -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC"))


def _causal_minutes(minute_bars: pd.DataFrame, hour_start, window_end):
    if len(minute_bars) == 0:
        return minute_bars
    index = minute_bars.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        return None
    index = index.tz_convert("UTC")
    frame = minute_bars.copy()
    frame.index = index
    kept = frame.loc[(frame.index >= hour_start) & (frame.index < window_end)].sort_index()
    return kept[~kept.index.duplicated(keep="last")]


def _last_bar(hourly, as_of):
    if hourly is None or len(hourly) == 0 or as_of is None:
        return None
    stamp = _as_utc(as_of).floor("min")
    index = hourly.index
    if not isinstance(index, pd.DatetimeIndex):
        return None
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    position = int(index.searchsorted(stamp, side="right")) - 1
    if position < 0:
        return None
    row = hourly.iloc[position]
    row = row.copy()
    row.name = index[position]
    return row


def _signal(hourly_open, hourly_close):
    opened = _finite(hourly_open)
    closed = _finite(hourly_close)
    if opened is None or closed is None:
        return None
    if closed > opened:
        return "UP"
    if closed < opened:
        return "DOWN"
    return "FLAT"


def _direction_counts(window: pd.DataFrame, signal):
    if signal == "FLAT":
        return 0, 0
    if signal not in {"UP", "DOWN"}:
        return None, None
    with_signal = 0
    against_signal = 0
    for opened, closed in zip(window["open"], window["close"]):
        start = _finite(opened)
        end = _finite(closed)
        if start is None or end is None or end == start:
            continue
        minute_up = end > start
        agrees = minute_up if signal == "UP" else not minute_up
        if agrees:
            with_signal += 1
        else:
            against_signal += 1
    return with_signal, against_signal


def _lag_return(window: pd.DataFrame, minutes: int, hour_start):
    last_ts = window.index[-1]
    ref_ts = last_ts - pd.Timedelta(minutes=minutes)
    if ref_ts < hour_start or ref_ts not in window.index:
        return None
    return _ratio(float(window["close"].iloc[-1]) - float(window.loc[ref_ts, "close"]), float(window.loc[ref_ts, "close"]))


def _excursion(signal, first, high, low):
    if signal == "UP":
        return _ratio(high - first, first), _ratio(first - low, first)
    if signal == "DOWN":
        return _ratio(first - low, first), _ratio(high - first, first)
    return None, None


def _ratio(change, base):
    if base is None or base == 0 or change is None:
        return None
    if not pd.notna(change) or not pd.notna(base):
        return None
    return float(change) / float(base)


def _finite(value):
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return number


def _stored(value):
    if value is None:
        return None
    return value


def _as_utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")

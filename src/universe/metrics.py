"""Assemble Universe v1 metrics from daily and hourly OHLCV.

Daily bars feed the long-term trend (EMA200) and ADV20.
Hourly bars feed RSI(14), MACD histogram, ATR(14), ATR%, and VolumeRatio.

The daily trend uses only sessions that started before the scan's
America/New_York calendar day. The in-progress session is excluded so a
same-day daily close cannot leak into EMA200.

Hourly indicators are computed on bars at or before ``as_of``. A bar up to
two hours older than ``as_of`` may stand in when that exact timestamp is
missing. Older hourly data is left blank rather than presented as current.
"""

import math
from typing import Optional

import pandas as pd

from src.data.provider import prepare_ohlcv
from src.features.indicators import atr, avg_dollar_volume, ema, macd_hist, rsi, volume_ratio

HOURLY_STALE_AFTER = pd.Timedelta(hours=2)
EMA_SPAN = 200
ADV_WINDOW = 20
RSI_WINDOW = 14
ATR_WINDOW = 14
VOLUME_WINDOW = 20


def as_utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def to_utc_iso(value) -> str:
    return as_utc(value).floor("min").isoformat()


def _num(value) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def completed_daily(daily: pd.DataFrame, as_of) -> pd.DataFrame:
    """Daily bars whose New York session started before ``as_of``'s session."""
    frame = prepare_ohlcv(daily)
    if frame.empty:
        return frame
    session_start = as_utc(as_of).tz_convert("America/New_York").normalize()
    et_index = frame.index.tz_convert("America/New_York")
    return frame.loc[et_index < session_start]


def _empty_metrics(as_of_ts: pd.Timestamp) -> dict:
    return {
        "scan_timestamp": as_of_ts,
        "hourly_close": None,
        "daily_close": None,
        "daily_ema200": None,
        "rsi14": None,
        "macd_hist": None,
        "atr14": None,
        "atr_percent": None,
        "volume": None,
        "volume_ratio": None,
        "adv20": None,
    }


def precompute_symbol_metrics(daily: pd.DataFrame, hourly: pd.DataFrame) -> dict:
    """Indicator series for one symbol, computed once.

    ``daily`` and ``hourly`` must already be ``prepare_ohlcv`` frames.
    RSI, MACD, ATR, VolumeRatio, EMA200, and ADV20 are causal, so the value
    at a bar equals the value of that same bar on any later prefix. Callers
    still have to sample with ``metrics_as_of`` so a snapshot at T does not
    read a later bar.
    """
    prepared = {
        "hourly": hourly,
        "daily": daily,
        "rsi": None,
        "macd": None,
        "atr": None,
        "volume_ratio": None,
        "daily_et": None,
        "ema": None,
        "adv": None,
    }
    if hourly is not None and not hourly.empty:
        prepared["rsi"] = rsi(hourly, RSI_WINDOW)
        prepared["macd"] = macd_hist(hourly)
        prepared["atr"] = atr(hourly, ATR_WINDOW)
        prepared["volume_ratio"] = volume_ratio(hourly, VOLUME_WINDOW)
    if daily is not None and not daily.empty:
        prepared["daily_et"] = daily.index.tz_convert("America/New_York")
        prepared["ema"] = ema(daily["close"], EMA_SPAN)
        prepared["adv"] = avg_dollar_volume(daily, ADV_WINDOW)
    return prepared


def metrics_as_of(prepared: dict, as_of) -> dict:
    """Read one causal snapshot from series built by ``precompute_symbol_metrics``."""
    as_of_ts = as_utc(as_of).floor("min")
    metrics = _empty_metrics(as_of_ts)
    hourly = prepared["hourly"]
    if hourly is not None and not hourly.empty:
        position = int(hourly.index.searchsorted(as_of_ts, side="right")) - 1
        if position >= 0 and as_of_ts - hourly.index[position] <= HOURLY_STALE_AFTER:
            close = _num(hourly["close"].iloc[position])
            if close is not None and close <= 0:
                close = None
            volume = _num(hourly["volume"].iloc[position])
            if volume is not None and volume < 0:
                volume = None
            metrics["hourly_close"] = close
            metrics["volume"] = volume
            metrics["rsi14"] = _num(prepared["rsi"].iloc[position])
            metrics["macd_hist"] = _num(prepared["macd"].iloc[position])
            atr14 = _num(prepared["atr"].iloc[position])
            metrics["atr14"] = atr14
            if atr14 is not None and close not in (None, 0):
                metrics["atr_percent"] = atr14 / close
            metrics["volume_ratio"] = _num(prepared["volume_ratio"].iloc[position])

    daily = prepared["daily"]
    if daily is not None and not daily.empty:
        session_start = as_of_ts.tz_convert("America/New_York").normalize()
        position = int(prepared["daily_et"].searchsorted(session_start, side="left")) - 1
        if position >= 0:
            metrics["daily_close"] = _num(daily["close"].iloc[position])
            if position + 1 >= EMA_SPAN:
                metrics["daily_ema200"] = _num(prepared["ema"].iloc[position])
            if position + 1 >= ADV_WINDOW:
                metrics["adv20"] = _num(prepared["adv"].iloc[position])
    return metrics


def compute_symbol_metrics(daily: pd.DataFrame, hourly: pd.DataFrame, as_of) -> dict:
    """Latest causal metrics for one symbol at ``as_of``."""
    prepared = precompute_symbol_metrics(prepare_ohlcv(daily), prepare_ohlcv(hourly))
    return metrics_as_of(prepared, as_of)

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


def compute_symbol_metrics(daily: pd.DataFrame, hourly: pd.DataFrame, as_of) -> dict:
    """Latest causal metrics for one symbol at ``as_of``."""
    as_of_ts = as_utc(as_of).floor("min")
    daily_frame = prepare_ohlcv(daily)
    hourly_frame = prepare_ohlcv(hourly)
    metrics = {
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

    hourly_hist = hourly_frame.loc[:as_of_ts] if not hourly_frame.empty else hourly_frame
    if not hourly_hist.empty and as_of_ts - hourly_hist.index[-1] <= HOURLY_STALE_AFTER:
        bar = hourly_hist.iloc[-1]
        close = _num(bar["close"])
        if close is not None and close <= 0:
            close = None
        volume = _num(bar["volume"])
        if volume is not None and volume < 0:
            volume = None
        metrics["hourly_close"] = close
        metrics["volume"] = volume
        metrics["rsi14"] = _num(rsi(hourly_hist, RSI_WINDOW).iloc[-1])
        metrics["macd_hist"] = _num(macd_hist(hourly_hist).iloc[-1])
        atr14 = _num(atr(hourly_hist, ATR_WINDOW).iloc[-1])
        metrics["atr14"] = atr14
        if atr14 is not None and close not in (None, 0):
            metrics["atr_percent"] = atr14 / close
        metrics["volume_ratio"] = _num(volume_ratio(hourly_hist, VOLUME_WINDOW).iloc[-1])

    daily_hist = completed_daily(daily_frame, as_of_ts)
    if not daily_hist.empty:
        metrics["daily_close"] = _num(daily_hist["close"].iloc[-1])
        if len(daily_hist) >= EMA_SPAN:
            metrics["daily_ema200"] = _num(ema(daily_hist["close"], EMA_SPAN).iloc[-1])
        if len(daily_hist) >= ADV_WINDOW:
            metrics["adv20"] = _num(avg_dollar_volume(daily_hist, ADV_WINDOW).iloc[-1])
    return metrics

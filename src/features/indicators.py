from typing import Dict

import numpy as np
import pandas as pd


def momentum(df: pd.DataFrame, period: int = 1) -> pd.Series:
    return df["close"].pct_change(periods=period)


def ma_cross(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.Series:
    ma_fast = df["close"].rolling(fast).mean()
    ma_slow = df["close"].rolling(slow).mean()
    return (ma_fast - ma_slow) / ma_slow


def volatility(df: pd.DataFrame, window: int = 7) -> pd.Series:
    return df["close"].pct_change().rolling(window).std()


def avg_dollar_volume(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling mean of close * volume.

    Universe v1 ADV20 uses this on the daily series (see universe metrics).
    """
    return (df["close"] * df["volume"]).rolling(window).mean()


def volume_ratio(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Current volume / rolling mean of volume.

    The window includes the current bar. This is a participation ratio, not a
    spike signal. A zero mean (no volume in the window) yields 0.
    """
    mean = df["volume"].rolling(window).mean()
    ratio = df["volume"] / mean.where(mean != 0)
    return ratio.mask(mean == 0, 0.0)


def volume_spike(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Legacy name kept for the pre-Universe weighted scorer.

    Prefer ``volume_ratio`` in Universe v1. This alias still adds a tiny
    epsilon in the denominator so existing callers keep their previous scale.
    """
    return df["volume"] / (df["volume"].rolling(window).mean() + 1e-9)


def ema(close: pd.Series, span: int = 200) -> pd.Series:
    """Recursive EMA of a price series.

    ``adjust=False`` uses alpha = 2 / (span + 1), seeded with the first price.
    Universe v1 daily trend uses ``ema(close, 200)`` computed here, not an
    external indicator feed.
    """
    return close.ewm(span=span, adjust=False).mean()


def rsi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Cutler's RSI: SMA of gains and losses over ``window`` bars.

    Universe v1 hourly RSI(14) uses this implementation. It is not Wilder's
    recursive RSI. A zero average loss uses a tiny epsilon so a run of gains
    approaches 100 instead of dividing by zero.
    """
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.rolling(window).mean()
    ma_down = down.rolling(window).mean()
    rs = ma_up / (ma_down + 1e-9)
    return 100 - (100 / (1 + rs))


def macd_hist(df: pd.DataFrame, a: int = 12, b: int = 26, c: int = 9) -> pd.Series:
    """MACD histogram: EMA(a) - EMA(b), minus the EMA(c) signal line.

    EMAs use ``adjust=False``. Universe v1 keeps these standard parameters
    (12, 26, 9) on the hourly series.
    """
    ema_a = df["close"].ewm(span=a, adjust=False).mean()
    ema_b = df["close"].ewm(span=b, adjust=False).mean()
    macd = ema_a - ema_b
    signal = macd.ewm(span=c, adjust=False).mean()
    hist = macd - signal
    return hist


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder ATR.

    True range is the max of high-low, |high - previous close|, and
    |low - previous close|. Smoothing is the recursive Wilder RMA
    ``ewm(alpha=1/window, adjust=False)``, seeded with the first true range
    (there is no SMA seed). Universe v1 uses hourly ATR(14).
    """
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    true_range = true_range.copy()
    if len(true_range):
        true_range.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return true_range.ewm(alpha=1 / window, adjust=False).mean()


def atr_percent(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """ATR divided by the bar close. The volatility veto uses this ratio."""
    return atr(df, window=window) / df["close"]


def distance_to_high(df: pd.DataFrame, window: int = 30) -> pd.Series:
    high = df["close"].rolling(window).max()
    return (high - df["close"]) / (high + 1e-9)


def compute_all_features(df: pd.DataFrame) -> Dict[str, pd.Series]:
    feats = {}
    feats["mom_1h"] = momentum(df, period=1)
    feats["mom_24h"] = momentum(df, period=24) if "close" in df.columns and len(df) > 24 else momentum(df, period=1)
    feats["ma_cross"] = ma_cross(df, fast=50, slow=200)
    feats["volatility"] = volatility(df, window=7)
    feats["adv"] = avg_dollar_volume(df, window=20)
    feats["vol_spike"] = volume_spike(df, window=20)
    feats["rsi"] = rsi(df, window=14)
    feats["macd_hist"] = macd_hist(df)
    feats["dist_high"] = distance_to_high(df, window=30)
    # Not available. NaN must not be read as a real earnings/news/short signal.
    # The legacy weighted sum still coerces NaN to 0; Universe v1 does not use these.
    feats["earnings_proximity"] = pd.Series(np.nan, index=df.index)
    feats["news_sentiment"] = pd.Series(np.nan, index=df.index)
    feats["short_interest"] = pd.Series(np.nan, index=df.index)
    return feats


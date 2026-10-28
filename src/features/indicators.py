import pandas as pd


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


def ema(close: pd.Series, span: int = 200) -> pd.Series:
    """Recursive EMA of a price series.

    ``adjust=False`` uses alpha = 2 / (span + 1), seeded with the first price.
    Universe v1 daily trend uses ``ema(close, 200)`` computed here, not an
    external indicator feed.
    """
    return close.ewm(span=span, adjust=False).mean()


def rsi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Cutler's RSI: SMA of gains and losses over ``window`` bars.

    Universe v1 passes ``window=28`` and a 30-minute frame. The default window
    is 14. This is not
    Wilder's recursive RSI. A zero average loss uses a tiny epsilon so a run
    of gains approaches 100 instead of dividing by zero.
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

    EMAs use ``adjust=False``. Universe v1 passes 24, 52, and 18 on the
    30-minute frame. The default parameters are 12, 26, and 9.
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
    (there is no SMA seed). Universe v1 passes ``window=28`` on the 30-minute
    frame. The default window is 14.
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



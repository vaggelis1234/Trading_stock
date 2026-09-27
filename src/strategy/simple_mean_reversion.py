from typing import Optional

import pandas as pd
import numpy as np


def mean_reversion_signals(df: pd.DataFrame, window: int = 20, z_entry: float = 1.5, z_exit: float = 0.5, vol_window: int = 20, min_volume: Optional[float] = None) -> pd.Series:
    # Generate conservative mean-reversion signals.
    # - Compute rolling mean and std on close
    # - z = (close - mean)/std
    # - Enter long when z < -z_entry and volume > min_volume and volatility below threshold
    # - Exit when z > -z_exit (i.e., mean reversion)
    # Returns Series of {1,0} where 1 = long, 0 = flat.
    df = df.copy()
    close = df["close"]
    ma = close.rolling(window).mean()
    std = close.rolling(window).std()
    z = (close - ma) / std
    vol = df.get("volume", pd.Series(np.nan, index=df.index))
    vol_ma = vol.rolling(vol_window).mean()

    signals = pd.Series(0, index=df.index, dtype=int)
    in_position = False
    for ts in df.index:
        if pd.isna(z.loc[ts]) or pd.isna(vol_ma.loc[ts]):
            signals.loc[ts] = 0
            continue
        # volatility filter: skip if std is very high (conservative)
        if std.loc[ts] / ma.loc[ts] > 0.05:
            signals.loc[ts] = 0
            in_position = False
            continue
        if not in_position:
            if z.loc[ts] < -z_entry and (min_volume is None or vol_ma.loc[ts] > min_volume):
                signals.loc[ts] = 1
                in_position = True
            else:
                signals.loc[ts] = 0
        else:
            # exit condition
            if z.loc[ts] > -z_exit:
                signals.loc[ts] = 0
                in_position = False
            else:
                signals.loc[ts] = 1
    return signals


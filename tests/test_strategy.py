import pandas as pd
import numpy as np

from src.strategy.simple_mean_reversion import mean_reversion_signals


def test_mean_reversion_basic():
    dates = pd.date_range("2020-01-01", periods=30, freq="D")
    # create synthetic close that drops then reverts
    close = np.concatenate([np.linspace(100, 80, 10), np.linspace(80, 100, 20)])
    vol = np.full(len(close), 1000.0)
    df = pd.DataFrame({"close": close, "volume": vol}, index=dates)
    signals = mean_reversion_signals(df, window=5, z_entry=1.0, z_exit=0.2, vol_window=5, min_volume=10)
    # Expect at least one entry and one exit
    assert signals.sum() >= 1
    assert signals.isin([0, 1]).all()


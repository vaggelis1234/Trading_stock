import pandas as pd
import numpy as np

from src.scoring.engine import rank_universe, load_weights


def test_rank_universe_simple():
    dates = pd.date_range("2022-01-01", periods=10, freq="D")
    close = np.linspace(100, 110, len(dates))
    vol = np.full(len(dates), 1000.0)
    df = pd.DataFrame({"close": close, "volume": vol}, index=dates)
    res = rank_universe({"A": df, "B": df}, load_weights())
    assert isinstance(res, list)
    assert len(res) == 2


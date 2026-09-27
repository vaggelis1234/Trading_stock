import pandas as pd
import numpy as np

from src.backtest.engine import PortfolioBacktestEngine


def test_portfolio_basic():
    dates = pd.date_range("2021-01-01", periods=10, freq="D")
    # two symbols
    close_a = np.linspace(100, 110, len(dates))
    close_b = np.linspace(50, 55, len(dates))
    vol = np.full(len(dates), 1000.0)
    df_a = pd.DataFrame({"close": close_a, "volume": vol}, index=dates)
    df_b = pd.DataFrame({"close": close_b, "volume": vol}, index=dates)
    data = {"A": df_a, "B": df_b}
    # signals: buy A on day 1, buy B on day 3
    sig_a = pd.Series(0, index=dates)
    sig_a.iloc[1] = 1
    sig_b = pd.Series(0, index=dates)
    sig_b.iloc[3] = 1
    signals = {"A": sig_a, "B": sig_b}
    engine = PortfolioBacktestEngine(data, cash=100000, position_fraction=0.1, per_symbol_liquidity={"A": 0.1, "B": 0.05})
    res = engine.run(signals)
    eq = res["equity_curve"]
    assert not eq.empty
    assert "final_cash" in res

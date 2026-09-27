\"\"\"Example script to run a simple backtest end-to-end using the provided modules.\"\"\"
from datetime import datetime, timedelta
import os
import pandas as pd

from src.data.ingest import fetch_historical_yfinance
from src.strategy.simple_mean_reversion import mean_reversion_signals
from src.backtest.engine import BacktestEngine


def main():
    symbol = os.getenv("BACKTEST_SYMBOL", "AAPL")
    end = datetime.utcnow().date()
    start = end - timedelta(days=365)
    df = fetch_historical_yfinance(symbol, start=start.isoformat(), end=end.isoformat(), interval="1d")
    if df.empty:
        print(\"No data fetched for\", symbol)
        return
    signals = mean_reversion_signals(df, window=20, z_entry=1.5, z_exit=0.5, vol_window=20)
    engine = BacktestEngine(df, cash=100_000, slippage=0.0005, commission=1.0)
    result = engine.run(signals)
    eq = result["equity_curve"]
    trades = result["trades"]
    print(\"Final cash:\", result["final_cash"])
    print(\"Trades executed:\", len(trades))
    out = \"artifacts\"
    os.makedirs(out, exist_ok=True)
    eq.to_csv(os.path.join(out, f\"equity_{symbol}.csv\"))
    print(\"Equity curve saved to\", os.path.join(out, f\"equity_{symbol}.csv\"))


if __name__ == \"__main__\":
    main()


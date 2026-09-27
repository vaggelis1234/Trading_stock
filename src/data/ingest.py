import os
from typing import Optional

import pandas as pd

# Simple historical fetch via yfinance
def fetch_historical_yfinance(symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
    # Fetch historical OHLCV data via yfinance as a fallback/historical source.
    # Args:
    #   symbol: ticker symbol, e.g. 'AAPL'
    #   start: YYYY-MM-DD
    #   end: YYYY-MM-DD
    #   interval: '1d', '1m', '5m', etc.
    # Returns:
    #   DataFrame with DatetimeIndex and columns: open, high, low, close, volume
    try:
        import yfinance as yf
    except Exception as e:
        raise RuntimeError("yfinance is required for historical fetch") from e

    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end, interval=interval)
    if df.empty:
        return pd.DataFrame()
    # normalize column names
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]]


# Minimal Alpaca connector for paper mode.
class AlpacaIngest:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET")
        self.base_url = base_url or os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        self.client = None
        try:
            import alpaca_trade_api as tradeapi
            self.client = tradeapi.REST(self.api_key, self.api_secret, base_url=self.base_url, api_version='v2')
        except Exception:
            # leave client None for environments without the dependency
            self.client = None

    def get_historical(self, symbol: str, start: str, end: str, timeframe: str = "1D") -> pd.DataFrame:
        # Return historical bars via Alpaca when available, else raise RuntimeError.
        if not self.client:
            raise RuntimeError("Alpaca client not configured or dependency missing")
        bars = self.client.get_bars(symbol, timeframe, start=start, end=end).df
        if bars.empty:
            return pd.DataFrame()
        # Alpaca bars multi-index may include symbol; normalize
        if isinstance(bars.columns, pd.MultiIndex):
            # try to select columns for symbol
            try:
                bars = bars[symbol]
            except Exception:
                bars = bars.iloc[:, 0:5]
        bars = bars.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        bars.index = pd.to_datetime(bars.index)
        return bars[["open", "high", "low", "close", "volume"]]


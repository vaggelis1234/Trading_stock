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


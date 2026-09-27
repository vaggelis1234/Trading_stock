"""Market-data seam for Universe v1.

Metric, veto, scoring, and ranking code depend only on OHLCV frames.
``YFinanceMarketDataProvider`` is the historical default and delegates to the
existing ``fetch_historical_yfinance`` helper. A later Alpaca-backed provider
can implement ``get_bars`` with the same columns and replace this class at the
pipeline boundary.
"""

from typing import Protocol

import pandas as pd

from src.data.ingest import fetch_historical_yfinance

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class MarketDataProvider(Protocol):
    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        """Return OHLCV bars for ``interval`` (``1d`` or ``1h``)."""


def prepare_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize a provider frame to UTC OHLCV.

    Naive timestamps are interpreted as UTC. Rows are sorted, floored to the
    minute, and de-duplicated so hourly scans align across symbols.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    out = df.copy()
    out.columns = [str(column).lower() for column in out.columns]
    missing = [column for column in OHLCV_COLUMNS if column not in out.columns]
    if missing:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    out = out.loc[:, OHLCV_COLUMNS]
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index, utc=True)).floor("min")
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out = out.dropna(subset=["close"])
    return out


class YFinanceMarketDataProvider:
    """Historical OHLCV via the existing yfinance ingest helper."""

    def get_bars(self, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
        raw = fetch_historical_yfinance(symbol, start=start, end=end, interval=interval)
        return prepare_ohlcv(raw)

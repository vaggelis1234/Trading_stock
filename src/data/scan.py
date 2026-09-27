import os
from typing import List, Dict

import pandas as pd
import yfinance as yf


def fetch_sp500_symbols() -> List[str]:
    # Fetch S&P500 symbols via yfinance's Ticker module (uses wiki table).
    try:
        table = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = table[0]
        symbols = df["Symbol"].tolist()
        return symbols
    except Exception:
        # fallback: small hardcoded sample
        return ["AAPL", "MSFT", "AMZN", "GOOGL", "FB", "TSLA", "NVDA", "JPM", "V", "JNJ"]


def top_n_by_marketcap(symbols: List[str], n: int = 100) -> List[str]:
    # Return top-n symbols by market cap using yfinance info (may be slow).
    metas = []
    for s in symbols:
        try:
            t = yf.Ticker(s)
            info = t.info
            mc = info.get("marketCap", 0) or 0
            metas.append((s, mc))
        except Exception:
            metas.append((s, 0))
    metas.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in metas[:n]]


def build_universe_top_sp500(n: int = 100) -> List[str]:
    symbols = fetch_sp500_symbols()
    top = top_n_by_marketcap(symbols, n=n)
    return top


import io
import logging
from typing import List, Optional

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

SP500_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FULL_UNIVERSE_MIN_SYMBOLS = 400
USER_AGENT = "TradeStocks/0.1 (S&P 500 constituent resolution)"


class UniverseResolutionError(RuntimeError):
    """The S&P 500 constituent list could not be resolved."""


def _symbol_column(frame: pd.DataFrame):
    for column in frame.columns:
        name = column[-1] if isinstance(column, tuple) else column
        if str(name).strip() == "Symbol":
            return column
    return None


def yahoo_symbol(symbol: str) -> str:
    """Map a Wikipedia ticker to the Yahoo/yfinance symbol.

    Share classes use a dot on Wikipedia (``BRK.B``, ``BF.B``) and a hyphen
    on Yahoo (``BRK-B``, ``BF-B``). Other tickers are unchanged.
    """
    return str(symbol).strip().upper().replace(".", "-")


def _clean_symbols(values) -> List[str]:
    symbols: List[str] = []
    seen = set()
    for raw in values:
        if raw is None or pd.isna(raw):
            continue
        symbol = yahoo_symbol(raw)
        if not symbol or symbol == "NAN" or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def symbols_from_constituent_tables(tables) -> List[str]:
    """Pick the Symbol column that holds the constituent list.

    A short table is rejected. It is not reported as the S&P 500 universe.
    """
    best: List[str] = []
    for table in tables:
        column = _symbol_column(table)
        if column is None:
            continue
        symbols = _clean_symbols(table[column].tolist())
        if len(symbols) > len(best):
            best = symbols
    if len(best) < FULL_UNIVERSE_MIN_SYMBOLS:
        raise UniverseResolutionError(
            f"Constituent table has {len(best)} symbols, which is not the full S&P 500 universe. "
            "No sample list was substituted."
        )
    return best


def _download_constituent_html(url: str = SP500_CONSTITUENTS_URL) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def fetch_sp500_symbols() -> List[str]:
    """Return the current S&P 500 constituents.

    Wikipedia blocks the default urllib client with HTTP 403, so the page is
    requested with an identified User-Agent and parsed locally. A failed or
    short response raises ``UniverseResolutionError`` instead of substituting
    a sample list.
    """
    try:
        html = _download_constituent_html()
        tables = pd.read_html(io.StringIO(html))
        symbols = symbols_from_constituent_tables(tables)
    except UniverseResolutionError:
        raise
    except Exception as exc:
        raise UniverseResolutionError(
            "S&P 500 constituent fetch failed. No sample list was substituted. "
            f"Cause: {exc}"
        ) from exc
    logger.info("resolved full S&P 500 constituent list: %s symbols", len(symbols))
    return symbols


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


def build_sp500_universe(limit: Optional[int] = None) -> List[str]:
    """S&P 500 evaluation pool.

    ``limit`` truncates the constituent list. It does not sort by market cap.
    The score ranking, including Top 100, happens after every symbol is scored.
    """
    symbols = list(fetch_sp500_symbols())
    if limit is None:
        return symbols
    if limit <= 0:
        raise ValueError("universe pool size must be positive")
    if limit < len(symbols):
        logger.info(
            "S&P 500 evaluation pool capped to %s of %s constituents in list order; the cap is not the full universe",
            limit,
            len(symbols),
        )
    return symbols[:limit]


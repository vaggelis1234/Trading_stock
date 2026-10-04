import inspect

import pandas as pd
import pytest
import requests

from src.data.scan import (
    FULL_UNIVERSE_MIN_SYMBOLS,
    UniverseResolutionError,
    fetch_sp500_symbols,
    symbols_from_constituent_tables,
    yahoo_symbol,
)
import src.data.scan as scan


def _symbols(count):
    symbols = [f"S{index:03d}" for index in range(count)]
    symbols[0] = "BRK.B"
    symbols[1] = "META"
    return symbols


def _html(symbols):
    rows = "".join(f"<tr><td>{symbol}</td></tr>" for symbol in symbols)
    return "<table><thead><tr><th>Symbol</th></tr></thead>" f"<tbody>{rows}</tbody></table>"


class _Response:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP Error {self.status_code}: Forbidden")


def test_constituent_table_resolves_the_full_symbol_list():
    symbols = _symbols(FULL_UNIVERSE_MIN_SYMBOLS)
    resolved = symbols_from_constituent_tables(
        [
            pd.DataFrame({"Symbol": ["AAPL", "MSFT"]}),
            pd.DataFrame({"Symbol": symbols}),
        ]
    )
    assert resolved[0] == "BRK-B"
    assert resolved[1] == "META"
    assert len(resolved) == FULL_UNIVERSE_MIN_SYMBOLS


def test_fetch_parses_a_full_constituent_page(monkeypatch):
    symbols = _symbols(503)
    monkeypatch.setattr(
        "src.data.scan.requests.get",
        lambda *args, **kwargs: _Response(200, _html(symbols)),
    )
    assert fetch_sp500_symbols() == [yahoo_symbol(symbol) for symbol in symbols]


def test_http_failure_raises_and_does_not_substitute_a_sample(monkeypatch):
    monkeypatch.setattr(
        "src.data.scan.requests.get",
        lambda *args, **kwargs: _Response(403, "blocked"),
    )
    with pytest.raises(UniverseResolutionError, match="No sample list was substituted") as caught:
        fetch_sp500_symbols()
    message = str(caught.value)
    assert "403" in message
    assert "FB" not in message


def test_short_table_is_not_treated_as_the_full_universe():
    frame = pd.DataFrame(
        {"Symbol": ["AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA", "JPM", "V", "JNJ"]}
    )
    with pytest.raises(UniverseResolutionError, match="not the full S&P 500 universe") as caught:
        symbols_from_constituent_tables([frame])
    assert "No sample list was substituted" in str(caught.value)
    assert "FB" not in str(caught.value)


def test_yahoo_symbol_normalizes_share_classes_and_leaves_plain_tickers():
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("BF.B") == "BF-B"
    assert yahoo_symbol("AAPL") == "AAPL"


def test_normalized_share_classes_stay_in_the_universe_and_dedup():
    symbols = _symbols(FULL_UNIVERSE_MIN_SYMBOLS)
    symbols[2] = "BF.B"
    symbols[3] = "AAPL"
    symbols.append("BRK-B")
    symbols.append("BF.B")
    resolved = symbols_from_constituent_tables([pd.DataFrame({"Symbol": symbols})])
    assert resolved[0] == "BRK-B"
    assert resolved[2] == "BF-B"
    assert "AAPL" in resolved
    assert "BRK.B" not in resolved
    assert "BF.B" not in resolved
    assert resolved.count("BRK-B") == 1
    assert resolved.count("BF-B") == 1
    assert len(resolved) == FULL_UNIVERSE_MIN_SYMBOLS


def test_constituent_source_has_no_obsolete_fb_fallback():
    source = inspect.getsource(scan)
    assert '"FB"' not in source
    assert "'FB'" not in source

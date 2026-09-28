import pytest


@pytest.fixture(autouse=True)
def isolated_market_bars_db(tmp_path, monkeypatch):
    """Keep raw-bar caches out of a developer's artifacts/market_bars.db."""
    monkeypatch.setenv("MARKET_BARS_DB", str(tmp_path / "market_bars.db"))

"""Local S&P 500 Security names for the symbol page.

The map is the Wikipedia constituent list, stored next to this module.
A missing file or an unknown ticker leaves the name empty. The page still
shows the symbol.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

_NAMES_PATH = Path(__file__).with_name("sp500_names.json")


@lru_cache(maxsize=1)
def company_names() -> dict[str, str]:
    if not _NAMES_PATH.is_file():
        return {}
    loaded = json.loads(_NAMES_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {str(symbol): str(name) for symbol, name in loaded.items() if name}


def company_name(symbol: str) -> Optional[str]:
    """Security name for a scan ticker, or None when the list has no match."""
    return company_names().get(str(symbol).strip().upper())

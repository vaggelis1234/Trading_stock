"""Earnings-date boundary for the hard veto.

Universe v1 does not ship a reliable earnings calendar. The default provider
reports itself unavailable and returns no dates. Callers must not treat that
as "no earnings soon" and must not block, or pass, on a fabricated zero.
"""

from typing import Optional, Protocol

import pandas as pd


class EarningsProvider(Protocol):
    available: bool

    def within_hours(self, symbol: str, as_of: pd.Timestamp, hours: int = 48) -> Optional[bool]:
        """True when earnings fall inside the window, False when they do not.

        Return None when this symbol has no reliable date.
        """


class UnavailableEarningsProvider:
    """Placeholder until a real earnings calendar is integrated."""

    available = False

    def within_hours(self, symbol: str, as_of: pd.Timestamp, hours: int = 48) -> Optional[bool]:
        return None


def earnings_within_48h(provider: Optional[EarningsProvider], symbol: str, as_of: pd.Timestamp) -> Optional[bool]:
    """Resolve the veto input.

    None means the check is inactive for this symbol. Only an explicit True
    from an available provider can produce BLOCKED_EARNINGS.
    """
    if provider is None or not provider.available:
        return None
    return provider.within_hours(symbol, as_of, hours=48)

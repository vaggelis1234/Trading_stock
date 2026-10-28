"""Buy approval is a user flag. It is not an entry rule and not an order.

Stage 2 (buy execution, validations, limit orders, risk checks, and position
sizing) is not implemented. ``ENTRY_RULE_DEFINED`` stays the watch engine's
own fact. Neither flag alone can make a buy active.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.watch.engine import ENTRY_RULE_DEFINED

NO_BUY = "no_buy"
YES_BUY = "yes_buy"
TRADING_EXECUTION_IMPLEMENTED = False


class BuyPermissionError(Exception):
    """``yes_buy`` was requested without both required services."""


@dataclass(frozen=True)
class LaunchOptions:
    enable_scheduler: bool = False
    enable_watch_engine: bool = False
    buy_permission: str = NO_BUY

    @property
    def services_requested(self) -> bool:
        return self.enable_scheduler or self.enable_watch_engine


@dataclass(frozen=True)
class TradingState:
    buy_permission: str
    entry_rule_defined: bool
    trading_execution: str
    buys_active: bool


def resolve_buy_permission(*, enable_scheduler: bool, enable_watch_engine: bool, yes_buy: bool) -> str:
    """Return ``no_buy`` or ``yes_buy``. Refuse approval when a service is off."""
    if not yes_buy:
        return NO_BUY
    if not enable_scheduler or not enable_watch_engine:
        raise BuyPermissionError(
            "yes_buy requires --enable_scheduler and --enable_watch_engine"
        )
    return YES_BUY


def trading_state(permission: str) -> TradingState:
    """Backend view of buy execution. Approval does not turn buys on."""
    if permission not in (NO_BUY, YES_BUY):
        permission = NO_BUY
    implemented = TRADING_EXECUTION_IMPLEMENTED and ENTRY_RULE_DEFINED
    return TradingState(
        buy_permission=permission,
        entry_rule_defined=ENTRY_RULE_DEFINED,
        trading_execution="available" if implemented else "unavailable",
        buys_active=permission == YES_BUY and implemented,
    )

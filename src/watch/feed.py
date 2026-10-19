"""Real-time quotes for the watch list.

Historical Universe bars stay on Alpaca SIP, which this Basic subscription
delivers with a 15-minute delay. The watch list therefore uses the IEX
stream (``wss://stream.data.alpaca.markets/v2/iex``). IEX is not labeled SIP.

A quiet symbol is not a disconnect. The connection is stale when the stream
itself stops sending. Symbols past the Basic stream cap are named and left
unsubscribed; they are not dropped without a record.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

IEX_STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"
FEED_SOURCE = "iex"
BASIC_IEX_STREAM_SYMBOL_LIMIT = 30
# Alpaca does not emit a separate heartbeat on an idle IEX socket. Any
# connection message refreshes liveness. One quiet name does not.
STALE_AFTER = pd.Timedelta(seconds=90)
_LIVENESS_TYPES = frozenset({"success", "subscription", "error", "t", "q", "b", "d"})


@dataclass(frozen=True)
class SubscriptionPlan:
    source: str
    subscribed: tuple[str, ...]
    omitted: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.omitted


def plan_subscription(positions: list[str], candidates: list[str], limit: int = BASIC_IEX_STREAM_SYMBOL_LIMIT) -> SubscriptionPlan:
    """Positions first, then candidates. Overflow is returned, not hidden."""
    required = list(dict.fromkeys([*positions, *candidates]))
    if len(required) <= limit:
        return SubscriptionPlan(FEED_SOURCE, tuple(required), ())
    return SubscriptionPlan(FEED_SOURCE, tuple(required[:limit]), tuple(required[limit:]))


class IexFeed:
    """IEX stream health. Tests drive it through ``ingest``; ``connect`` is optional."""

    source = FEED_SOURCE

    def __init__(self, stale_after: pd.Timedelta = STALE_AFTER, symbol_limit: int = BASIC_IEX_STREAM_SYMBOL_LIMIT):
        self.stale_after = stale_after
        self.symbol_limit = symbol_limit
        self.url = IEX_STREAM_URL
        self.connected = False
        self.authenticated = False
        self.health_confirmed = False
        self.coverage_complete = True
        self.required: tuple[str, ...] = ()
        self.subscribed: frozenset[str] = frozenset()
        self.omitted: tuple[str, ...] = ()
        self.last_message_at = None
        self.last_trade_at: dict[str, pd.Timestamp] = {}
        self.last_trade_price: dict[str, float] = {}

    def set_required(self, positions: list[str], candidates: list[str]) -> SubscriptionPlan:
        plan = plan_subscription(positions, candidates, self.symbol_limit)
        self.required = plan.subscribed
        self.omitted = plan.omitted
        self.coverage_complete = plan.complete
        if self.connected:
            self.health_confirmed = self.authenticated and self._subscription_matches()
        return plan

    def connect(self) -> None:
        """Mark the socket open. Health stays unconfirmed until IEX authenticates and subscribes."""
        self.connected = True
        self.authenticated = False
        self.health_confirmed = False
        self.subscribed = frozenset()

    def disconnect(self) -> None:
        self.connected = False
        self.health_confirmed = False

    def ingest(self, payload, now) -> None:
        """Apply one IEX message or a list of messages. Does not open a socket."""
        moment = _aware(now)
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            kind = str(message.get("T", ""))
            if kind not in _LIVENESS_TYPES:
                continue
            self.last_message_at = moment
            if kind == "success" and message.get("msg") == "authenticated":
                self.authenticated = True
            elif kind == "subscription":
                self.subscribed = frozenset(message.get("trades") or [])
                self.health_confirmed = self.connected and self.authenticated and self._subscription_matches()
            elif kind == "t":
                symbol = str(message.get("S", ""))
                if symbol:
                    self.last_trade_at[symbol] = moment
                    price = message.get("p")
                    if price is not None:
                        self.last_trade_price[symbol] = float(price)

    def healthy(self, now) -> bool:
        """True only after a confirmed IEX subscription that is still fresh."""
        if not self.connected or not self.authenticated or not self.health_confirmed:
            return False
        if not self.coverage_complete:
            return False
        if self.last_message_at is None:
            return False
        moment = _aware(now)
        if moment - self.last_message_at > self.stale_after:
            return False
        return self._subscription_matches()

    def price(self, symbol: str):
        return self.last_trade_price.get(symbol)

    def _subscription_matches(self) -> bool:
        return set(self.required).issubset(self.subscribed)


def _aware(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("feed clock must be timezone-aware")
    return stamp

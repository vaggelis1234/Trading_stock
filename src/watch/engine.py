"""Watch list and open-position monitor.

The engine starts after the first successful regular Universe scan and keeps
evaluating until the cash session closes. It does not stop between hourly
scans. The 16:15 final scan does not start it and does not open a buy cycle.

Universe v1 has no entry trigger. ``TradingBotManager.choose_and_enter`` buys
the top legacy score; that is not an Universe entry rule, so buys stay
disabled. The prototype bot also exits on a 2% stop, a 4% target, and a
24-hour hold. The 24-hour hold is an outside-session policy. No Universe
session exit rule is defined, so this monitor records order state and does
not submit exits.
"""

from __future__ import annotations

import logging
from datetime import time

import pandas as pd

from src.risk.manager import RiskManager
from src.universe.minute_candidates import select_minute_candidates
from src.universe.schedule import FINAL, as_new_york
from src.watch.feed import IexFeed
from src.watch.orders import OrderStore

logger = logging.getLogger(__name__)

ENTRY_RULE_DEFINED = False
ENTRY_RULE_GAP = (
    "Universe v1 scores and ranks symbols and does not define an entry trigger. "
    "Buys stay disabled until that rule is specified."
)
EXIT_RULE_GAP = (
    "No Universe session exit rule is defined. "
    "The prototype 24-hour hold is outside the cash session and is not applied. "
    "Open positions are polled for status only; no exit order is submitted."
)
PAPER_URL = "https://paper-api.alpaca.markets"
ABSOLUTE_BUY_CUTOFF = time(16, 0)


class PositionMonitor:
    """Poll working orders every session minute. Does not invent an exit."""

    exit_rule_defined = False

    def __init__(self, orders: OrderStore, adapter):
        self.orders = orders
        self.adapter = adapter
        self.polls: list[str] = []
        self.exit_submissions = 0
        logger.warning("%s", EXIT_RULE_GAP)

    def tick(self, now) -> None:
        """Refresh status during the session. Never submits an order."""
        del now
        for order in self.orders.open_orders():
            broker_id = order.get("broker_id") or ""
            if not broker_id:
                continue
            remote = self.adapter.get_order(broker_id)
            self.orders.apply_remote(order["id"], remote)
            self.polls.append(broker_id)


class BuyGate:
    """Last check before a paper buy. The 16:00 ET cutoff does not trust the engine."""

    def __init__(self, orders: OrderStore, adapter, risk: RiskManager, entry_rule_defined: bool = ENTRY_RULE_DEFINED):
        self.orders = orders
        self.adapter = adapter
        self.risk = risk
        self.entry_rule_defined = entry_rule_defined
        self.last_block: list[str] = []
        self.submitted: list[str] = []

    def block_reasons(self, now, *, feed: IexFeed, snapshot_safe: bool, session_close: time) -> list[str]:
        moment = as_new_york(now)
        reasons = []
        if moment.time() >= ABSOLUTE_BUY_CUTOFF:
            reasons.append("after_1600_et")
        if session_close is not None and moment.time() >= session_close:
            reasons.append("session_closed")
        if feed is None or not feed.healthy(moment):
            reasons.append("feed_unhealthy")
        if not snapshot_safe:
            reasons.append("snapshot_unsafe")
        if not self.entry_rule_defined:
            reasons.append("entry_rule_unspecified")
        if not self.risk.allow_new_trade():
            reasons.append("risk_blocked")
        return reasons

    def submit_buy(self, row: dict, now, qty: float, price: float, *, feed: IexFeed, snapshot_safe: bool, session_close: time):
        moment = as_new_york(now)
        reasons = self.block_reasons(moment, feed=feed, snapshot_safe=snapshot_safe, session_close=session_close)
        if moment.time() >= ABSOLUTE_BUY_CUTOFF and "after_1600_et" not in reasons:
            reasons.append("after_1600_et")
        if reasons:
            self.last_block = reasons
            return None
        if not _paper_endpoint(self.adapter):
            self.last_block = ["live_trading_disabled"]
            return None
        symbol = str(row["symbol"])
        session_date = moment.date()
        if price is None or price <= 0 or qty <= 0:
            self.last_block = ["no_price"]
            return None
        if qty > self.risk.max_position_size(price):
            self.last_block = ["risk_position_limit"]
            return None
        if qty * price > self.risk.cash:
            self.last_block = ["insufficient_capital"]
            return None
        if not self.orders.reserve(symbol, session_date, qty, price):
            self.last_block = ["duplicate_order"]
            return None
        try:
            order = self.adapter.submit_limit_order(symbol=symbol, qty=qty, side="buy", limit_price=price)
        except Exception:
            logger.exception("buy submit failed for %s; the symbol stays blocked", symbol)
            self.orders.mark(symbol, session_date, "submit_uncertain")
            self.last_block = ["submit_uncertain"]
            return None
        broker_id = str(order.get("id", "")) if isinstance(order, dict) else ""
        filled = float(order.get("filled_qty") or 0) if isinstance(order, dict) else 0
        status = "filled" if isinstance(order, dict) and order.get("status") == "filled" else "submitted"
        self.orders.mark(symbol, session_date, status, broker_id, filled)
        self.submitted.append(symbol)
        self.last_block = []
        return order


class WatchEngine:
    """Minute evaluation of the latest Universe candidates."""

    def __init__(self, orders: OrderStore, adapter, risk: RiskManager | None = None, feed: IexFeed | None = None):
        self.orders = orders
        self.adapter = adapter
        self.risk = risk or RiskManager(cash=100000)
        self.feed = feed or IexFeed()
        self.buy_gate = BuyGate(self.orders, adapter, self.risk)
        self.positions = PositionMonitor(self.orders, adapter)
        self.order_sizer = None
        self.candidates: list[dict] = []
        self.started = False
        self.evaluating = False
        self.snapshot_safe_for_buys = False
        self.session_close = time(16, 0)
        self.evaluations: list[pd.Timestamp] = []
        self.pause_count = 0
        self.pause_hook = None
        self._paused = False
        self._queue: list[pd.Timestamp] = []
        self._evaluated: set[str] = set()
        self.final_scan_seen = False
        logger.warning("%s", ENTRY_RULE_GAP)

    def on_scan_success(self, slot, rows: list[dict]) -> None:
        if slot.kind == FINAL:
            self.final_scan_seen = True
            logger.info("final universe scan %s updated history and did not open a buy cycle", slot.slot_id)
            return
        self._refresh(rows, slot.session_date)

    def on_scan_failed(self, slot) -> None:
        self.snapshot_safe_for_buys = False
        logger.warning(
            "universe scan %s failed; last candidate list stays for monitoring and new buys are blocked",
            getattr(slot, "slot_id", slot),
        )

    def resume(self, rows: list[dict]) -> None:
        """Restore today's candidates after a restart. Does not run a scan."""
        if self.started or not rows:
            return
        self._refresh(rows, _session_day(rows))

    def tick(self, now, session_close: time) -> None:
        self.session_close = session_close
        moment = as_new_york(now)
        if _session_ended(moment, session_close):
            self.evaluating = False
            self.positions.tick(moment)
            return
        if self.started and self.evaluating:
            self.evaluate_minute(moment)
        self.positions.tick(moment)

    def evaluate_minute(self, now):
        moment = as_new_york(now).floor("min")
        if _session_ended(moment, self.session_close):
            self.evaluating = False
            return None
        if self._paused:
            self._queue.append(moment)
            return None
        if not self.started or not self.evaluating:
            return None
        key = moment.isoformat()
        if key in self._evaluated:
            return None
        self._evaluated.add(key)
        self.evaluations.append(moment)
        reasons = self.buy_gate.block_reasons(
            moment,
            feed=self.feed,
            snapshot_safe=self.snapshot_safe_for_buys,
            session_close=self.session_close,
        )
        if reasons or self.order_sizer is None:
            self.buy_gate.last_block = reasons
            return reasons
        for row in list(self.candidates):
            price = self.feed.price(row["symbol"])
            if price is None:
                logger.info("no IEX trade yet for %s; the quiet symbol is not treated as a disconnect", row["symbol"])
                continue
            qty = self.order_sizer(row, price)
            if not qty:
                continue
            order = self.buy_gate.submit_buy(
                row,
                moment,
                qty=qty,
                price=price,
                feed=self.feed,
                snapshot_safe=self.snapshot_safe_for_buys,
                session_close=self.session_close,
            )
            if order is not None:
                self.candidates = [item for item in self.candidates if item["symbol"] != row["symbol"]]
        self._sync_feed()
        return None

    def _refresh(self, rows: list[dict], session_date) -> None:
        self._paused = True
        self.pause_count += 1
        try:
            if self.pause_hook is not None:
                self.pause_hook()
            selected = select_minute_candidates(rows)
            blocked = self.orders.blocked_symbols(session_date)
            self.candidates = [row for row in selected if row["symbol"] not in blocked]
            self.snapshot_safe_for_buys = True
            self.started = True
            self.evaluating = True
            self._sync_feed()
        except Exception:
            logger.exception("candidate refresh failed; new buys stay blocked")
            self.snapshot_safe_for_buys = False
        finally:
            queued = list(dict.fromkeys(self._queue))
            self._queue.clear()
            self._paused = False
        for moment in queued:
            self.evaluate_minute(moment)

    def _sync_feed(self) -> None:
        positions = self.orders.position_symbols()
        names = [row["symbol"] for row in self.candidates]
        plan = self.feed.set_required(positions, names)
        if plan.omitted:
            for symbol in plan.omitted:
                logger.warning(
                    "IEX subscription omitted %s; Basic symbol limit is %s. New buys are blocked.",
                    symbol,
                    self.feed.symbol_limit,
                )


def _session_ended(moment: pd.Timestamp, session_close: time) -> bool:
    wall = moment.time()
    return wall >= ABSOLUTE_BUY_CUTOFF or (session_close is not None and wall >= session_close)


def _paper_endpoint(adapter) -> bool:
    url = str(getattr(adapter, "base_url", "") or "")
    return url.startswith(PAPER_URL)


def _session_day(rows: list[dict]):
    stamp = pd.Timestamp(rows[0].get("scan_timestamp"))
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York").date()

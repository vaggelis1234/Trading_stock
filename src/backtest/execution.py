from typing import List, Dict, Any
import uuid

# Simple volume-aware execution simulator.
# - liquidity_fraction: fraction of bar volume available to our orders (e.g. 0.1 => 10% of volume)
# - impact_coeff: extra price impact applied linearly with filled fraction relative to available volume
# - slippage_pct: baseline slippage applied to executed price
# Orders persist across bars until filled or cancelled.
class ExecutionSimulator:
    def __init__(self, liquidity_fraction: float = 0.1, impact_coeff: float = 0.2, slippage_pct: float = 0.0005):
        self.liquidity_fraction = liquidity_fraction
        self.impact_coeff = impact_coeff
        self.slippage_pct = slippage_pct
        self.open_orders: Dict[str, Dict[str, Any]] = {}

    def submit_limit_order(self, symbol: str, qty: float, side: str, limit_price: float, time_in_force: str = "gtc") -> str:
        oid = str(uuid.uuid4())
        self.open_orders[oid] = {
            "id": oid,
            "symbol": symbol,
            "qty": qty,
            "remaining": qty,
            "side": side,
            "limit_price": limit_price,
            "tif": time_in_force,
        }
        return oid

    def submit_market_order(self, symbol: str, qty: float, side: str) -> str:
        # represent market order as limit with extremely wide limit
        oid = str(uuid.uuid4())
        self.open_orders[oid] = {
            "id": oid,
            "symbol": symbol,
            "qty": qty,
            "remaining": qty,
            "side": side,
            "limit_price": None,
            "tif": "day",
        }
        return oid

    def cancel_order(self, order_id: str):
        if order_id in self.open_orders:
            del self.open_orders[order_id]

    def process_bar(self, symbol: str, bar: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Attempt to fill open orders for the symbol on this bar.
        # bar must contain: open, high, low, close, volume
        # Returns list of fills: {order_id, filled_qty, filled_price, side}
        fills: List[Dict[str, Any]] = []
        # total available volume for our orders this bar
        available_volume = max(0.0, float(bar.get("volume", 0.0))) * self.liquidity_fraction
        if available_volume <= 0:
            return fills

        # process orders FIFO
        for oid, order in list(self.open_orders.items()):
            if order["symbol"] != symbol:
                continue
            if available_volume <= 0:
                break
            remaining = order["remaining"]
            # check if limit allows execution at this bar's close (simple model)
            price = float(bar.get("close", 0.0))
            if order["limit_price"] is not None:
                # only fill if limit is favorable
                if order["side"] == "buy" and order["limit_price"] < price:
                    # limit below current price - cannot fill at this bar (conservative)
                    continue
                if order["side"] == "sell" and order["limit_price"] > price:
                    continue
            # determine fillable qty
            fill_qty = min(remaining, available_volume)
            # price impact: linear with fraction of available executed
            frac = fill_qty / (available_volume if available_volume > 0 else 1.0)
            impact = 1.0 + self.impact_coeff * frac if order["side"] == "buy" else 1.0 - self.impact_coeff * frac
            exec_price = price * impact
            # apply baseline slippage
            if order["side"] == "buy":
                exec_price = exec_price * (1 + self.slippage_pct)
            else:
                exec_price = exec_price * (1 - self.slippage_pct)

            # record fill
            fills.append({"order_id": oid, "filled_qty": fill_qty, "filled_price": exec_price, "side": order["side"]})
            order["remaining"] -= fill_qty
            available_volume -= fill_qty
            if order["remaining"] <= 1e-9:
                # order fully filled
                del self.open_orders[oid]

        return fills

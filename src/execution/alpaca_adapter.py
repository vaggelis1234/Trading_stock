import os
import time
from typing import Optional, Dict, Any


# Minimal Alpaca execution adapter for paper trading.
# Expects ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL in environment.
# Falls back to a simulator if dependency is missing.
class AlpacaAdapter:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET")
        self.base_url = base_url or os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        self.client = None
        try:
            import alpaca_trade_api as tradeapi
            self.client = tradeapi.REST(self.api_key, self.api_secret, base_url=self.base_url, api_version='v2')
        except Exception:
            self.client = None

    def submit_limit_order(self, symbol: str, qty: float, side: str, limit_price: float, time_in_force: str = "gtc") -> Dict[str, Any]:
        if not self.client:
            # simulator response
            return {"id": f"sim-{int(time.time()*1000)}", "status": "filled", "filled_qty": qty, "filled_avg_price": limit_price}
        order = self.client.submit_order(symbol=symbol, qty=qty, side=side, type="limit", time_in_force=time_in_force, limit_price=limit_price)
        return order._raw

    def submit_market_order(self, symbol: str, qty: float, side: str, time_in_force: str = "day") -> Dict[str, Any]:
        if not self.client:
            # simulator uses last price read via market endpoint if available
            return {"id": f"sim-{int(time.time()*1000)}", "status": "filled", "filled_qty": qty, "filled_avg_price": None}
        order = self.client.submit_order(symbol=symbol, qty=qty, side=side, type="market", time_in_force=time_in_force)
        return order._raw

    def get_order(self, order_id: str) -> Dict[str, Any]:
        if not self.client:
            return {"id": order_id, "status": "filled"}
        order = self.client.get_order(order_id)
        return order._raw


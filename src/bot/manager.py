import os
from typing import Optional
from datetime import datetime, timedelta

from src.execution.alpaca_adapter import AlpacaAdapter
from src.risk.manager import RiskManager
from src.scoring.engine import load_weights, rank_universe
from src.data.scan import build_universe_top_sp500
import yfinance as yf
import pandas as pd


class TradingBotManager:
    def __init__(self, cash: float = 100000, allocation_pct: float = 0.05):
        self.cash = cash
        self.allocation_pct = allocation_pct
        self.risk = RiskManager(cash=cash)
        self.adapter = AlpacaAdapter()
        self.position = None  # track current open position symbol
        self.position_entry_time: Optional[datetime] = None
        self.position_entry_price: Optional[float] = None

    def choose_and_enter(self, symbol_to_df: dict):
        weights = load_weights("config/weights.yaml")
        scores = rank_universe(symbol_to_df, weights)
        if not scores:
            return None
        top = scores[0]
        sym = top["symbol"]
        price = top["price"]
        # allocation
        notional = self.cash * self.allocation_pct
        qty = int(notional / price) if price and price > 0 else 0
        if qty <= 0:
            return None
        # submit limit slightly above bid (here use price as placeholder)
        order = self.adapter.submit_limit_order(symbol=sym, qty=qty, side="buy", limit_price=price * 1.001)
        self.position = sym
        self.position_entry_time = datetime.utcnow()
        self.position_entry_price = price
        return order

    def monitor_and_exit(self, symbol_to_df: dict):
        if not self.position:
            return None
        sym = self.position
        df = symbol_to_df.get(sym)
        if df is None or df.empty:
            return None
        latest_price = float(df["close"].iloc[-1])
        # exit conditions: simple stoploss/takeprofit/time
        if latest_price <= self.position_entry_price * 0.98:
            # stoploss 2%
            order = self.adapter.submit_limit_order(symbol=sym, qty=0, side="sell", limit_price=latest_price * 0.999)
            self._clear_position()
            return order
        if latest_price >= self.position_entry_price * 1.04:
            order = self.adapter.submit_limit_order(symbol=sym, qty=0, side="sell", limit_price=latest_price * 0.999)
            self._clear_position()
            return order
        if datetime.utcnow() - self.position_entry_time > timedelta(hours=24):
            order = self.adapter.submit_limit_order(symbol=sym, qty=0, side="sell", limit_price=latest_price * 0.999)
            self._clear_position()
            return order
        return None

    def _clear_position(self):
        self.position = None
        self.position_entry_time = None
        self.position_entry_price = None


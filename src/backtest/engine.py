from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import pandas as pd

try:
    from src.persistence import store as persistence_store
except Exception:
    persistence_store = None

from src.backtest.execution import ExecutionSimulator


@dataclass
class Trade:
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float = 0.0
    entry_time: pd.Timestamp = None
    exit_time: pd.Timestamp = None
    pnl: float = 0.0


# Enhanced deterministic backtester with configurable slippage & commission models.
class BacktestEngine:
    def __init__(
        self,
        df: pd.DataFrame,
        cash: float = 100_000,
        slippage_pct: float = 0.0,
        commission_per_share: float = 0.0,
        commission_per_order: float = 0.0,
        position_fraction: float = 0.1,
        persistence_db: Optional[str] = None,
    ):
        # df must contain DatetimeIndex and columns: open, high, low, close, volume
        self.df = df.sort_index()
        self.starting_cash = cash
        self.cash = cash
        self.slippage_pct = slippage_pct
        self.commission_per_share = commission_per_share
        self.commission_per_order = commission_per_order
        self.position_fraction = position_fraction
        self.position = 0.0
        self.trades: List[Trade] = []
        self.persistence_db = persistence_db
        # execution simulator for more realistic fills
        self.exec_sim = ExecutionSimulator(liquidity_fraction=0.1, impact_coeff=0.2, slippage_pct=self.slippage_pct)

    def _apply_slippage(self, price: float, side: str) -> float:
        # simple slippage: add/subtract slippage_pct of price depending on side
        if self.slippage_pct == 0:
            return price
        if side == "buy":
            return price * (1 + self.slippage_pct)
        else:
            return price * (1 - self.slippage_pct)

    def _commission(self, qty: float) -> float:
        return self.commission_per_order + self.commission_per_share * abs(qty)

    def run(self, signals: pd.Series) -> Dict[str, Any]:
        # signals: Series indexed like df, values: 1 (long), 0 (flat)
        equity_curve = []
        pending_order_id: Optional[str] = None
        for ts, row in self.df.iterrows():
            price = float(row["close"])
            sig = signals.get(ts, 0) if isinstance(signals, pd.Series) else 0
            # decide orders based on signal transitions
            if sig == 1 and self.position == 0 and pending_order_id is None:
                qty = (self.cash * self.position_fraction) / price
                # submit market order (will be filled by exec_sim across bars)
                pending_order_id = self.exec_sim.submit_market_order(symbol="", qty=qty, side="buy")
            elif sig == 0 and self.position > 0 and pending_order_id is None:
                qty = self.position
                pending_order_id = self.exec_sim.submit_market_order(symbol="", qty=qty, side="sell")

            # process fills for this bar
            fills = self.exec_sim.process_bar(symbol="", bar=row.to_dict())
            # apply fills to account
            for f in fills:
                filled_qty = f["filled_qty"]
                filled_price = f["filled_price"]
                side = f["side"]
                commission = self._commission(filled_qty)
                if side == "buy":
                    cost = filled_qty * filled_price + commission
                    # protect against insufficient cash due to rounding
                    if cost > self.cash:
                        # scale down fill to available cash
                        max_qty = max(0.0, (self.cash - commission) / filled_price)
                        actual_qty = max_qty
                        cost = actual_qty * filled_price + commission
                        self.position += actual_qty
                        self.cash -= cost
                        self.trades.append(Trade(symbol="", side="buy", qty=actual_qty, entry_price=filled_price, entry_time=ts))
                    else:
                        self.position += filled_qty
                        self.cash -= cost
                        self.trades.append(Trade(symbol="", side="buy", qty=filled_qty, entry_price=filled_price, entry_time=ts))
                else:
                    proceeds = filled_qty * filled_price - commission
                    self.cash += proceeds
                    # close trades FIFO
                    remaining = filled_qty
                    while remaining > 1e-9 and self.trades:
                        last = self.trades[-1]
                        if last.exit_time is not None:
                            # already closed
                            self.trades.pop()
                            continue
                        # close portion
                        close_qty = min(last.qty, remaining)
                        last.exit_price = filled_price
                        last.exit_time = ts
                        last.pnl = (last.exit_price - last.entry_price) * close_qty - commission
                        last.qty -= close_qty
                        remaining -= close_qty
                        if last.qty <= 1e-9:
                            # fully closed
                            self.trades.pop()
            equity = self.cash + self.position * price
            equity_curve.append({"timestamp": ts, "equity": equity})
        eq_df = pd.DataFrame(equity_curve).set_index("timestamp")

        # persist trades if persistence available
        if persistence_store and self.trades and self.persistence_db:
            rows = []
            for t in self.trades:
                rows.append(
                    {
                        "symbol": t.symbol,
                        "side": t.side,
                        "qty": t.qty,
                        "entry_price": t.entry_price,
                        "exit_price": t.exit_price,
                        "entry_time": t.entry_time,
                        "exit_time": t.exit_time,
                        "pnl": t.pnl,
                    }
                )
            persistence_store.init_db(self.persistence_db)
            persistence_store.save_trades(rows, path=self.persistence_db)

        return {"equity_curve": eq_df, "trades": self.trades, "final_cash": self.cash}


class PortfolioBacktestEngine:
    """Portfolio-level backtester for multiple symbols with per-symbol liquidity."""

    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        cash: float = 100_000,
        slippage_pct: float = 0.0,
        commission_per_share: float = 0.0,
        commission_per_order: float = 0.0,
        position_fraction: float = 0.1,
        persistence_db: Optional[str] = None,
        per_symbol_liquidity: Optional[Dict[str, float]] = None,
    ):
        # data: mapping symbol -> dataframe (with open,high,low,close,volume)
        self.data = {s: df.sort_index() for s, df in data.items()}
        self.starting_cash = cash
        self.cash = cash
        self.slippage_pct = slippage_pct
        self.commission_per_share = commission_per_share
        self.commission_per_order = commission_per_order
        self.position_fraction = position_fraction
        self.positions: Dict[str, float] = {s: 0.0 for s in self.data.keys()}
        self.trades: List[Trade] = []
        self.persistence_db = persistence_db
        self.per_symbol_liquidity = per_symbol_liquidity or {}
        # one execution simulator per symbol so liquidity_fraction can differ
        self.exec_sims: Dict[str, ExecutionSimulator] = {}
        for s in self.data.keys():
            lf = float(self.per_symbol_liquidity.get(s, 0.1))
            self.exec_sims[s] = ExecutionSimulator(liquidity_fraction=lf, impact_coeff=0.2, slippage_pct=self.slippage_pct)

    def _commission(self, qty: float) -> float:
        return self.commission_per_order + self.commission_per_share * abs(qty)

    def run(self, signals: Dict[str, pd.Series]) -> Dict[str, Any]:
        # build union of timestamps across all symbols
        all_ts = sorted({ts for df in self.data.values() for ts in df.index})
        equity_curve = []
        # pending orders tracked per symbol id
        pending_orders: Dict[str, Optional[str]] = {s: None for s in self.data.keys()}

        for ts in all_ts:
            # submit orders based on signals at ts for each symbol
            for s, df in self.data.items():
                row = None
                if ts in df.index:
                    row = df.loc[ts]
                sig_series = signals.get(s, pd.Series(dtype=int))
                sig = int(sig_series.get(ts, 0)) if sig_series is not None else 0
                # submit entry
                price = float(row["close"]) if row is not None else None
                if sig == 1 and self.positions[s] == 0 and pending_orders[s] is None and price is not None:
                    qty = (self.cash * self.position_fraction) / price
                    oid = self.exec_sims[s].submit_market_order(symbol=s, qty=qty, side="buy")
                    pending_orders[s] = oid
                elif sig == 0 and self.positions[s] > 0 and pending_orders[s] is None and price is not None:
                    qty = self.positions[s]
                    oid = self.exec_sims[s].submit_market_order(symbol=s, qty=qty, side="sell")
                    pending_orders[s] = oid

            # process fills per symbol for this timestamp
            for s, df in self.data.items():
                row = None
                if ts in df.index:
                    row = df.loc[ts]
                if row is None:
                    continue
                fills = self.exec_sims[s].process_bar(symbol=s, bar=row.to_dict())
                for f in fills:
                    filled_qty = f["filled_qty"]
                    filled_price = f["filled_price"]
                    side = f["side"]
                    commission = self._commission(filled_qty)
                    if side == "buy":
                        cost = filled_qty * filled_price + commission
                        if cost > self.cash:
                            max_qty = max(0.0, (self.cash - commission) / filled_price)
                            actual_qty = max_qty
                            cost = actual_qty * filled_price + commission
                            if actual_qty <= 0:
                                continue
                            self.positions[s] += actual_qty
                            self.cash -= cost
                            self.trades.append(Trade(symbol=s, side="buy", qty=actual_qty, entry_price=filled_price, entry_time=ts))
                        else:
                            self.positions[s] += filled_qty
                            self.cash -= cost
                            self.trades.append(Trade(symbol=s, side="buy", qty=filled_qty, entry_price=filled_price, entry_time=ts))
                    else:
                        proceeds = filled_qty * filled_price - commission
                        self.cash += proceeds
                        # close FIFO trades for this symbol
                        remaining = filled_qty
                        # find buys for this symbol in trades list (reverse)
                        for t in reversed(self.trades):
                            if t.symbol != s or t.side != "buy" or t.exit_time is not None:
                                continue
                            close_qty = min(t.qty, remaining)
                            t.exit_price = filled_price
                            t.exit_time = ts
                            t.pnl = (t.exit_price - t.entry_price) * close_qty - commission
                            t.qty -= close_qty
                            remaining -= close_qty
                            if t.qty <= 1e-9:
                                # fully closed, leave it with qty 0
                                pass
                            if remaining <= 1e-9:
                                break
                        self.positions[s] = max(0.0, self.positions[s] - filled_qty)

            # compute portfolio equity at ts
            total_equity = self.cash
            for s, df in self.data.items():
                if ts in df.index:
                    price = float(df.loc[ts]["close"])
                    total_equity += self.positions.get(s, 0.0) * price
            equity_curve.append({"timestamp": ts, "equity": total_equity})

        eq_df = pd.DataFrame(equity_curve).set_index("timestamp")

        # persist trades if requested
        if persistence_store and self.trades and self.persistence_db:
            rows = []
            for t in self.trades:
                rows.append(
                    {
                        "symbol": t.symbol,
                        "side": t.side,
                        "qty": t.qty,
                        "entry_price": t.entry_price,
                        "exit_price": t.exit_price,
                        "entry_time": t.entry_time,
                        "exit_time": t.exit_time,
                        "pnl": t.pnl,
                    }
                )
            persistence_store.init_db(self.persistence_db)
            persistence_store.save_trades(rows, path=self.persistence_db)

        return {"equity_curve": eq_df, "trades": self.trades, "final_cash": self.cash}


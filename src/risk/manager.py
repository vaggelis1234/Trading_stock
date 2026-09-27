from dataclasses import dataclass


def fixed_fraction_size(cash: float, price: float, fraction: float = 0.01) -> float:
    # Simple fixed-fraction position sizing: use `fraction` of cash per trade.
    size = (cash * fraction) / price
    return max(size, 0.0)


@dataclass
class RiskManager:
    cash: float
    max_position_percent: float = 0.2  # at most 20% of account on a single position
    daily_loss_limit: float = 0.02  # stop trading after losing 2% of starting account in a day
    starting_cash: float = None
    daily_losses: float = 0.0

    def __post_init__(self):
        if self.starting_cash is None:
            self.starting_cash = self.cash

    def max_position_size(self, price: float) -> float:
        # Return max quantity allowed given max_position_percent.
        max_notional = self.starting_cash * self.max_position_percent
        return max_notional / price

    def record_trade_pnl(self, pnl: float):
        self.daily_losses += max(0.0, -pnl)

    def allow_new_trade(self) -> bool:
        if self.daily_losses >= (self.starting_cash * self.daily_loss_limit):
            return False
        return True


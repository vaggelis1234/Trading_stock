import pandas as pd

from src.backtest.execution import ExecutionSimulator


def test_partial_fill():
    sim = ExecutionSimulator(liquidity_fraction=0.2, impact_coeff=0.1, slippage_pct=0.001)
    # submit buy for qty 1000
    oid = sim.submit_market_order(symbol="X", qty=1000, side="buy")
    # bar with volume 2000 -> available 400
    bar = {"open": 10, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 2000}
    fills = sim.process_bar("X", bar)
    assert len(fills) == 1
    assert fills[0]["filled_qty"] <= 400 + 1e-9
    # after first bar, order should remain
    assert oid in sim.open_orders
    # process second bar same volume -> order should fill fully now (remaining 600, available 400 -> partial, still open)
    fills2 = sim.process_bar("X", bar)
    # at least one fill occured
    assert len(fills2) >= 1


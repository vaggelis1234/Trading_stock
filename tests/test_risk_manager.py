from src.risk.manager import RiskManager, fixed_fraction_size


def test_fixed_fraction():
    size = fixed_fraction_size(100000, 50, fraction=0.01)
    assert size == (100000 * 0.01) / 50


def test_risk_manager_limits():
    rm = RiskManager(cash=100000, max_position_percent=0.2, daily_loss_limit=0.01)
    max_qty = rm.max_position_size(100)
    assert max_qty == (rm.starting_cash * rm.max_position_percent) / 100
    assert rm.allow_new_trade()
    rm.record_trade_pnl(-2000)
    # after a large loss, new trades should be blocked
    assert rm.allow_new_trade() is False


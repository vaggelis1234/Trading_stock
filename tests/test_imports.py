def test_imports():
    # smoke test: import core modules
    import importlib
    modules = [
        "src.data.ingest",
        "src.backtest.engine",
        "src.strategy.simple_mean_reversion",
        "src.risk.manager",
        "src.execution.alpaca_adapter",
    ]
    for m in modules:
        mod = importlib.import_module(m)
        assert mod is not None


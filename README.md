# Trade_stocks

Συντηρητική πλατφόρμα trading (paper) για μετοχές — πρωτότυπο για backtesting, signal generation, risk management και paper execution.

Βασικά σημεία
- Paper trading αρχικά (Alpaca paper adapter)
- Python-based stack: pandas, numpy, alpaca-trade-api, yfinance
- Modules: data ingest, backtester, strategy, risk, execution

Ξεκίνα:

1. Εγκατάσταση dependencies (συνιστώ `poetry` ή `python -m venv .venv` + `pip install -r requirements.txt`)
2. Δες `scripts/run_backtest.py` για παράδειγμα end-to-end backtest

Autodoc: αυτό το repository είναι scaffold — επεκτάσιμο και ασφαλές για δοκιμές σε paper mode.


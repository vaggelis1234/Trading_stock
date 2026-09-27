\"\"\"Simple equity report generator from CSV produced by run_backtest.py\"\"\"
import sys
import pandas as pd


def summarize(path: str):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    eq = df["equity"]
    ret = eq.pct_change().dropna()
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    annualized = (1 + total_ret) ** (252 / len(eq)) - 1 if len(eq) > 0 else 0
    print(f\"Periods: {len(eq)}\")
    print(f\"Total return: {total_ret:.2%}\")
    print(f\"Annualized (approx): {annualized:.2%}\")
    print(f\"Annual volatility (approx): {ret.std() * (252 ** 0.5):.2%}\")


if __name__ == \"__main__\":
    if len(sys.argv) < 2:
        print(\"Usage: python scripts/report_equity.py <equity_csv>\")
    else:
        summarize(sys.argv[1])


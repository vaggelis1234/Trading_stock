\"\"\"Generate detailed backtest report: metrics + plots saved to artifacts/\"\"\"
import json
import os
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def load_equity(path: str) -> pd.Series:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df["equity"]


def metrics_from_equity(eq: pd.Series) -> dict:
    rets = eq.pct_change().dropna()
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    annualized = (1 + total_ret) ** (252 / len(eq)) - 1 if len(eq) > 0 else 0
    vol = rets.std() * (252 ** 0.5)
    sharpe = (rets.mean() * 252) / vol if vol > 0 else 0
    dd = (eq.cummax() - eq).max()
    return {
        "periods": len(eq),
        "total_return": float(total_ret),
        "annualized_return": float(annualized),
        "annual_volatility": float(vol),
        "sharpe_approx": float(sharpe),
        "max_drawdown": float(dd),
    }


def plot_equity(eq: pd.Series, out_png: str):
    plt.figure(figsize=(8, 4))
    plt.plot(eq.index, eq.values)
    plt.title("Equity Curve")
    plt.xlabel("Date")
    plt.ylabel("Equity")
    plt.tight_layout()
    plt.savefig(out_png)
    plt.close()


def generate_report(equity_csv: str, out_dir: Optional[str] = "artifacts"):
    os.makedirs(out_dir, exist_ok=True)
    eq = load_equity(equity_csv)
    metrics = metrics_from_equity(eq)
    plot_equity(eq, os.path.join(out_dir, "equity.png"))
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    print(\"Report saved to\", out_dir)


if __name__ == \"__main__\":
    import sys

    if len(sys.argv) < 2:
        print(\"Usage: python scripts/report_backtest.py <equity_csv>\")
    else:
        generate_report(sys.argv[1])


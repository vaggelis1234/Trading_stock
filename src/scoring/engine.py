"""Legacy raw-feature weighted sum.

Universe v1 does not use this module. Its normalized 0-10 score lives in
``src/universe/scoring.py``. ``TradingBotManager`` still calls ``rank_universe``;
the Universe pipeline does not, and it does not place orders.
"""

import os
from typing import Dict, List

import numpy as np
import pandas as pd
import yaml

from src.features.indicators import compute_all_features


def load_weights(path: str = "config/weights.yaml") -> Dict[str, float]:
    if os.path.exists(path):
        with open(path, "r") as f:
            return yaml.safe_load(f)
    # defaults
    return {
        "mom_1h": 1.0,
        "mom_24h": 0.8,
        "ma_cross": 0.7,
        "volatility": -0.5,
        "adv": 0.5,
        "vol_spike": 0.6,
        "rsi": -0.3,
        "macd_hist": 0.9,
        "dist_high": 0.4,
        "earnings_proximity": -0.7,
        "news_sentiment": 0.8,
        "short_interest": -0.4,
    }


def winsorize_series(s: pd.Series, pct: float = 0.01) -> pd.Series:
    lo = s.quantile(pct)
    hi = s.quantile(1 - pct)
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / (s.std() + 1e-9)


def compute_score_for_symbol(df: pd.DataFrame, weights: Dict[str, float]) -> float:
    feats = compute_all_features(df)
    latest_idx = df.index[-1]
    vals = {}
    for k, ser in feats.items():
        v = ser.loc[latest_idx] if latest_idx in ser.index else np.nan
        vals[k] = v
    # build dataframe row
    row = pd.Series(vals)
    # winsorize & zscore per feature using single value fallback (here we rely on single symbol normalizer later)
    score = 0.0
    for f, w in weights.items():
        val = row.get(f, 0.0)
        if pd.isna(val):
            val = 0.0
        score += float(val) * float(w)
    return float(score)


def rank_universe(symbol_to_df: Dict[str, pd.DataFrame], weights: Dict[str, float]) -> List[Dict]:
    scores = []
    for s, df in symbol_to_df.items():
        try:
            score = compute_score_for_symbol(df, weights)
        except Exception:
            score = 0.0
        price = float(df["close"].iloc[-1]) if "close" in df.columns and len(df) > 0 else None
        scores.append({"symbol": s, "price": price, "score": score})
    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores


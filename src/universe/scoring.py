"""Universe v1 score. Ranking only. Eligible symbols are veto status PASS.

Normalization
-------------
Each factor is mapped to ``[0, 10]`` with a clamped linear scale, then combined
with the configured weights (default 30% / 30% / 20% / 20%). Weights sum to 1,
so the final score stays on ``[0, 10]``.

The caps below are scale anchors so dollar volume cannot dominate RSI. They are
not extra veto thresholds and they are not optimized entry rules.

Trend (weight 0.30)
    distance = (daily_close - daily_EMA200) / daily_EMA200
    0 at the EMA, 10 at 10% above the EMA, clamped.

Momentum (weight 0.30), equal blend of:
    RSI: 0 at 35, 10 at 75, clamped to the veto band.
    MACD histogram / hourly close: 0 at -2% of price, 10 at +2% of price.

Volatility risk (weight 0.20)
    Lower hourly ATR% scores higher inside the allowed band.
    10 at ATR% = 0, 0 at ATR% = 0.04, clamped.

Volume / liquidity (weight 0.20), equal blend of:
    VolumeRatio: 0 at 0.8, 10 at 2.0, clamped.
    ADV20: 0 at $5,000,000, 10 at 20x that level, on a log10 scale, clamped.

Blocked symbols are not scored. ``apply_score`` clears every score field
unless veto status is PASS.
"""

from pathlib import Path
import math
from typing import Dict, Mapping, Optional

import yaml

DEFAULT_WEIGHTS = {
    "trend": 0.30,
    "momentum": 0.30,
    "volatility_risk": 0.20,
    "volume_liquidity": 0.20,
}

TREND_DISTANCE_CAP = 0.10
MACD_HIST_PCT_CAP = 0.02
VOLUME_RATIO_FLOOR = 0.8
VOLUME_RATIO_CAP = 2.0
ATR_PERCENT_MAX = 0.04
RSI_MIN = 35.0
RSI_MAX = 75.0
ADV20_MIN = 5_000_000.0
ADV_MULTIPLE_CAP = 20.0

SCORE_FIELDS = (
    "trend_score",
    "momentum_score",
    "volatility_score",
    "volume_liquidity_score",
    "final_score",
)


def default_weights_path() -> Path:
    cwd = Path("config") / "universe_weights.yaml"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parents[2] / "config" / "universe_weights.yaml"


def load_universe_weights(path: Optional[str] = None) -> Dict[str, float]:
    weights_path = Path(path) if path else default_weights_path()
    if weights_path.exists():
        with weights_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    else:
        loaded = dict(DEFAULT_WEIGHTS)

    missing = [key for key in DEFAULT_WEIGHTS if key not in loaded]
    if missing:
        raise ValueError(f"universe weights missing keys: {', '.join(missing)}")

    weights = {key: float(loaded[key]) for key in DEFAULT_WEIGHTS}
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"universe factor weights must sum to 1, got {total}")
    return weights


def _clip01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _scale(value: float) -> float:
    return _clip01(value) * 10.0


def factor_scores(
    *,
    daily_close: float,
    daily_ema200: float,
    hourly_close: float,
    rsi14: float,
    macd_hist: float,
    atr_percent: float,
    volume_ratio: float,
    adv20: float,
    weights: Mapping[str, float],
) -> Dict[str, float]:
    """Return component scores and the weighted final score, each on [0, 10]."""
    distance = (daily_close - daily_ema200) / daily_ema200
    trend = _scale(distance / TREND_DISTANCE_CAP)

    rsi_component = _scale((rsi14 - RSI_MIN) / (RSI_MAX - RSI_MIN))
    macd_pct = macd_hist / hourly_close
    macd_component = _scale((macd_pct + MACD_HIST_PCT_CAP) / (2.0 * MACD_HIST_PCT_CAP))
    momentum = 0.5 * rsi_component + 0.5 * macd_component

    volatility = _scale((ATR_PERCENT_MAX - atr_percent) / ATR_PERCENT_MAX)

    volume_component = _scale((volume_ratio - VOLUME_RATIO_FLOOR) / (VOLUME_RATIO_CAP - VOLUME_RATIO_FLOOR))
    multiple = adv20 / ADV20_MIN
    if multiple <= 0:
        adv_component = 0.0
    else:
        adv_component = _scale(math.log10(multiple) / math.log10(ADV_MULTIPLE_CAP))
    volume_liquidity = 0.5 * volume_component + 0.5 * adv_component

    final = (
        weights["trend"] * trend
        + weights["momentum"] * momentum
        + weights["volatility_risk"] * volatility
        + weights["volume_liquidity"] * volume_liquidity
    )
    return {
        "trend_score": trend,
        "momentum_score": momentum,
        "volatility_score": volatility,
        "volume_liquidity_score": volume_liquidity,
        "final_score": final,
    }


def apply_score(row: Mapping, weights: Mapping[str, float]) -> dict:
    """Score PASS rows. Every other status gets empty score fields."""
    scored = dict(row)
    if scored.get("veto_status") != "PASS":
        for field in SCORE_FIELDS:
            scored[field] = None
        return scored

    scores = factor_scores(
        daily_close=float(scored["daily_close"]),
        daily_ema200=float(scored["daily_ema200"]),
        hourly_close=float(scored["hourly_close"]),
        rsi14=float(scored["rsi14"]),
        macd_hist=float(scored["macd_hist"]),
        atr_percent=float(scored["atr_percent"]),
        volume_ratio=float(scored["volume_ratio"]),
        adv20=float(scored["adv20"]),
        weights=weights,
    )
    scored.update(scores)
    return scored

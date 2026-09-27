"""Hard veto. First decision gate in Universe v1.

A failing rule blocks the symbol. The score cannot override that. When several
rules fail, ``reasons`` keeps every one of them and ``status`` is the earliest
rule in ``VETO_PRIORITY``.
"""

from dataclasses import dataclass
import math
from typing import Optional

PASS = "PASS"
BLOCKED_TREND = "BLOCKED_TREND"
BLOCKED_RSI = "BLOCKED_RSI"
BLOCKED_VOLATILITY = "BLOCKED_VOLATILITY"
BLOCKED_VOLUME = "BLOCKED_VOLUME"
BLOCKED_LIQUIDITY = "BLOCKED_LIQUIDITY"
BLOCKED_EARNINGS = "BLOCKED_EARNINGS"
BLOCKED_INSUFFICIENT_DATA = "BLOCKED_INSUFFICIENT_DATA"

VETO_PRIORITY = (
    BLOCKED_TREND,
    BLOCKED_RSI,
    BLOCKED_VOLATILITY,
    BLOCKED_VOLUME,
    BLOCKED_LIQUIDITY,
    BLOCKED_EARNINGS,
    BLOCKED_INSUFFICIENT_DATA,
)

RSI_MIN = 35.0
RSI_MAX = 75.0
ATR_PERCENT_MAX = 0.04
VOLUME_RATIO_MIN = 0.8
ADV20_MIN = 5_000_000.0


@dataclass(frozen=True)
class VetoResult:
    status: str
    reasons: tuple[str, ...]
    earnings_check: str


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def apply_hard_vetoes(
    *,
    daily_close,
    daily_ema200,
    rsi14,
    atr_percent,
    volume_ratio,
    adv20,
    earnings_within_48h: Optional[bool],
) -> VetoResult:
    """Evaluate every hard veto independently.

    ``earnings_within_48h`` is None when no reliable calendar is available.
    That does not pass or fail the earnings rule; the check is recorded as
    ``unavailable`` and BLOCKED_EARNINGS is not added.

    Missing or non-finite inputs skip that rule and add
    BLOCKED_INSUFFICIENT_DATA. Comparisons are never applied to missing data.
    """
    if earnings_within_48h is None:
        earnings_check = "unavailable"
    elif earnings_within_48h:
        earnings_check = "within_48h"
    else:
        earnings_check = "clear"

    failed: list[str] = []
    insufficient = False

    close = _num(daily_close)
    ema200 = _num(daily_ema200)
    if close is None or ema200 is None:
        insufficient = True
    elif close <= ema200:
        failed.append(BLOCKED_TREND)

    rsi = _num(rsi14)
    if rsi is None:
        insufficient = True
    elif rsi < RSI_MIN or rsi > RSI_MAX:
        failed.append(BLOCKED_RSI)

    atr_pct = _num(atr_percent)
    if atr_pct is None or atr_pct < 0:
        insufficient = True
    elif atr_pct > ATR_PERCENT_MAX:
        failed.append(BLOCKED_VOLATILITY)

    ratio = _num(volume_ratio)
    if ratio is None or ratio < 0:
        insufficient = True
    elif ratio < VOLUME_RATIO_MIN:
        failed.append(BLOCKED_VOLUME)

    dollar_volume = _num(adv20)
    if dollar_volume is None or dollar_volume < 0:
        insufficient = True
    elif dollar_volume < ADV20_MIN:
        failed.append(BLOCKED_LIQUIDITY)

    if earnings_within_48h is True:
        failed.append(BLOCKED_EARNINGS)

    if insufficient:
        failed.append(BLOCKED_INSUFFICIENT_DATA)

    if not failed:
        return VetoResult(PASS, (), earnings_check)
    return VetoResult(failed[0], tuple(failed), earnings_check)

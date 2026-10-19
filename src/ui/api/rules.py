"""Hardcoded Universe rules, read from the constants the scan already uses."""

from datetime import date

from src.market.calendar import REGULAR_CLOSE, REGULAR_OPEN, SessionHours
from src.universe.metrics import ADV_WINDOW, ATR_WINDOW, EMA_SPAN, HOURLY_STALE_AFTER, RSI_WINDOW, VOLUME_WINDOW
from src.universe.minute_candidates import max_candidates_for_1m
from src.universe.ranking import TOP_N
from src.universe.schedule import SIP_DELAY, session_slots
from src.universe.scoring import (
    ADV_MULTIPLE_CAP,
    ATR_PERCENT_MAX as SCORE_ATR_MAX,
    MACD_HIST_PCT_CAP,
    RSI_MAX as SCORE_RSI_MAX,
    RSI_MIN as SCORE_RSI_MIN,
    TREND_DISTANCE_CAP,
    VOLUME_RATIO_CAP,
    VOLUME_RATIO_FLOOR,
    load_universe_weights,
)
from src.universe.veto import ADV20_MIN, ATR_PERCENT_MAX, RSI_MAX, RSI_MIN, VOLUME_RATIO_MIN


def rules_payload() -> dict:
    """The veto, score, indicator, and session-window rules."""
    return {
        "groups": [
            _vetoes(),
            _weights(),
            _score_scale(),
            _indicators(),
            _windows(),
            _ranking(),
        ]
    }


def _vetoes() -> dict:
    return {
        "title": "Hard veto",
        "note": "A failing rule blocks the symbol. The score does not override it.",
        "rows": [
            {"name": "Trend", "value": "Daily close > EMA200"},
            {"name": "RSI", "value": f"{_num(RSI_MIN)} ≤ RSI({RSI_WINDOW}) ≤ {_num(RSI_MAX)}"},
            {"name": "Volatility", "value": f"ATR% ≤ {_num(ATR_PERCENT_MAX * 100)}%"},
            {"name": "Volume", "value": f"VolumeRatio ≥ {_num(VOLUME_RATIO_MIN)}"},
            {"name": "Liquidity", "value": f"ADV20 ≥ ${ADV20_MIN:,.0f}"},
            {"name": "Earnings", "value": "Block when earnings are within 48h. An unavailable calendar does not pass or fail."},
            {"name": "Insufficient data", "value": "Any required veto input is missing or not finite"},
        ],
    }


def _weights() -> dict:
    weights = load_universe_weights()
    return {
        "title": "Score weights",
        "note": "config/universe_weights.yaml. The weighted score stays on 0–10.",
        "rows": [
            {"name": "Trend", "value": _percent(weights["trend"])},
            {"name": "Momentum", "value": _percent(weights["momentum"])},
            {"name": "Volatility risk", "value": _percent(weights["volatility_risk"])},
            {"name": "Volume / liquidity", "value": _percent(weights["volume_liquidity"])},
        ],
    }


def _score_scale() -> dict:
    return {
        "title": "Score scale",
        "note": "Each factor is clamped to 0–10. These caps are scale anchors, not extra vetoes.",
        "rows": [
            {"name": "Trend", "value": f"0 at the EMA200, 10 at {_num(TREND_DISTANCE_CAP * 100)}% above it"},
            {"name": "Momentum RSI", "value": f"0 at {_num(SCORE_RSI_MIN)}, 10 at {_num(SCORE_RSI_MAX)}"},
            {"name": "Momentum MACD", "value": f"0 at -{_num(MACD_HIST_PCT_CAP * 100)}% of price, 10 at +{_num(MACD_HIST_PCT_CAP * 100)}%"},
            {"name": "Volatility", "value": f"10 at ATR% 0, 0 at ATR% {_num(SCORE_ATR_MAX * 100)}%"},
            {"name": "VolumeRatio", "value": f"0 at {_num(VOLUME_RATIO_FLOOR)}, 10 at {_num(VOLUME_RATIO_CAP)}"},
            {"name": "ADV20", "value": f"0 at ${ADV20_MIN:,.0f}, 10 at {_num(ADV_MULTIPLE_CAP)}× that level, on a log10 scale"},
        ],
    }


def _indicators() -> dict:
    stale_hours = int(HOURLY_STALE_AFTER.total_seconds() // 3600)
    return {
        "title": "Indicator windows",
        "note": "Daily trend uses completed sessions only. An hourly bar older than the stale limit is not used.",
        "rows": [
            {"name": "EMA", "value": f"{EMA_SPAN} daily closes"},
            {"name": "ADV", "value": f"{ADV_WINDOW} daily dollar-volume sessions"},
            {"name": "RSI", "value": f"{RSI_WINDOW} hourly bars"},
            {"name": "ATR", "value": f"{ATR_WINDOW} hourly bars"},
            {"name": "VolumeRatio", "value": f"{VOLUME_WINDOW} hourly bars"},
            {"name": "Hourly freshness", "value": f"{stale_hours} hours"},
        ],
    }


def _windows() -> dict:
    session = SessionHours(date(2026, 1, 5), REGULAR_OPEN, REGULAR_CLOSE)
    rows = [{"name": "SIP delay", "value": f"{int(SIP_DELAY.total_seconds() // 60)} minutes after the window ends"}]
    for slot in session_slots(session):
        start = slot.window_start.strftime("%H:%M")
        end = slot.window_end.strftime("%H:%M")
        ready = slot.scan_at.strftime("%H:%M")
        rows.append({"name": f"{start}–{end}", "value": f"Readable from {ready}"})
    return {
        "title": "Session windows",
        "note": "Regular cash session, America/New_York. Windows do not overlap. The last one is the closing half hour.",
        "rows": rows,
    }


def _ranking() -> dict:
    return {
        "title": "Ranking",
        "note": "PASS and BLOCKED symbols with a score are ranked together.",
        "rows": [
            {"name": "Dashboard", "value": f"Top {TOP_N} by score"},
            {"name": "Minute enrichment", "value": f"{max_candidates_for_1m()} highest PASS scores"},
        ],
    }


def _num(value: float) -> str:
    return f"{value:g}"


def _percent(weight: float) -> str:
    points = weight * 100
    if abs(points - round(points)) < 1e-9:
        return f"{int(round(points))}%"
    return f"{points:g}%"

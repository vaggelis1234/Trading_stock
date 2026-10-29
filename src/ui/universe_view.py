"""Rank labels, veto text, and snapshot coverage for the Universe API."""

from datetime import timedelta
from typing import Mapping, Optional, Sequence

import pandas as pd

from src.market.calendar import NyseCalendar
from src.universe.metrics import to_utc_iso
from src.universe.schedule import FINAL, SLOT_GRACE, as_new_york, session_slots
from src.universe.veto import (
    ADV20_MIN,
    ATR_PERCENT_MAX,
    BLOCKED_EARNINGS,
    BLOCKED_INSUFFICIENT_DATA,
    BLOCKED_LIQUIDITY,
    BLOCKED_RSI,
    BLOCKED_TREND,
    BLOCKED_VOLATILITY,
    BLOCKED_VOLUME,
    PASS,
    VOLUME_RATIO_MIN,
)

METRIC_FIELDS = {
    "RSI": "rsi14",
    "Score": "final_score",
    "ATR%": "atr_percent",
    "Volume Ratio": "volume_ratio",
}
HISTORY_RANGES = {
    "24H": pd.Timedelta(hours=24),
    "3D": pd.Timedelta(days=3),
    "5D": pd.Timedelta(days=5),
}
DEFAULT_HISTORY_RANGE = "24H"


def _adv_label(amount: float) -> str:
    millions = amount / 1_000_000.0
    if millions >= 1.0 and abs(millions - round(millions)) < 1e-6:
        return f"${int(round(millions))}M"
    return f"${amount:,.0f}"


def reason_text(code: str) -> str:
    """Human-readable veto reason. Thresholds come from the veto constants."""
    labels = {
        BLOCKED_VOLUME: f"VolumeRatio < {VOLUME_RATIO_MIN:g}",
        BLOCKED_RSI: "RSI outside allowed range",
        BLOCKED_VOLATILITY: f"ATR% > {ATR_PERCENT_MAX * 100.0:g}%",
        BLOCKED_LIQUIDITY: f"ADV20 < {_adv_label(ADV20_MIN)}",
        BLOCKED_TREND: "Price <= EMA200",
        BLOCKED_EARNINGS: "Earnings within 48h",
        BLOCKED_INSUFFICIENT_DATA: "Insufficient data",
    }
    return labels.get(str(code), str(code))


def display_status(veto_status) -> str:
    if veto_status == PASS:
        return "PASS"
    return "BLOCKED"


def rank_movement(current_rank, previous_rank) -> tuple[Optional[int], str]:
    """Return ``(previous_rank - current_rank, label)``.

    Positive movement means the rank improved. No previous rank is NEW.
    """
    if previous_rank is None:
        return None, "NEW"
    delta = int(previous_rank) - int(current_rank)
    if delta > 0:
        return delta, f"🟢 ↑{delta}"
    if delta < 0:
        return delta, f"🔴 ↓{abs(delta)}"
    return 0, "—"


def _hour_floor(value) -> pd.Timestamp:
    """Clock hour that contains a snapshot. ``17:02`` belongs to ``17:00``."""
    return pd.Timestamp(to_utc_iso(value)).floor("h")


def history_start(anchor, range_key: str = DEFAULT_HISTORY_RANGE) -> str:
    if range_key not in HISTORY_RANGES:
        raise ValueError(f"unknown history range: {range_key}")
    return to_utc_iso(_hour_floor(anchor) - HISTORY_RANGES[range_key])


def metric_display_value(row: Mapping, metric: str):
    """Stored metric for coverage. ATR% is percent points. Null stays null."""
    if metric not in METRIC_FIELDS:
        raise ValueError(f"unknown metric: {metric}")
    raw = row.get(METRIC_FIELDS[metric])
    if raw is None or pd.isna(raw):
        return None
    number = float(raw)
    if pd.isna(number):
        return None
    if metric == "ATR%":
        return number * 100.0
    return number


def expected_snapshot_timestamps(anchor, range_key: str = DEFAULT_HISTORY_RANGE) -> list[str]:
    """30-minute window starts that should already be stored, oldest first.

    A regular cash day has thirteen windows from 09:30 through 15:30–16:00,
    America/New_York. Weekends, holidays, and windows whose scan is still
    after ``anchor`` are not expected. An early close keeps only the windows
    that finish by that close.
    """
    start = _as_utc(history_start(anchor, range_key))
    moment = _as_utc(anchor)
    calendar = NyseCalendar()
    stamps = []
    day = start.tz_convert("America/New_York").date()
    last = moment.tz_convert("America/New_York").date()
    while day <= last:
        session = calendar.session(day)
        if session is not None:
            for slot in session_slots(session):
                window_start = slot.window_start.tz_convert("UTC")
                if window_start < start:
                    continue
                if slot.scan_at.tz_convert("UTC") > moment:
                    continue
                stamps.append(to_utc_iso(window_start))
        day += timedelta(days=1)
    return stamps


def snapshot_window_start(value) -> Optional[str]:
    """30-minute session window a stored snapshot belongs to.

    A scan clock within five minutes after its ready time still belongs to
    the window that just closed. Times outside the cash session return None.
    """
    ny = as_new_york(_as_utc(value))
    slots = session_slots(NyseCalendar().session(ny.date()))
    if not slots:
        return None
    for slot in slots:
        if slot.scan_at <= ny < slot.scan_at + SLOT_GRACE:
            return to_utc_iso(slot.window_start)
    for slot in slots:
        if slot.window_start <= ny < slot.window_end:
            return to_utc_iso(slot.window_start)
        if slot.kind == FINAL and ny == slot.window_end:
            return to_utc_iso(slot.window_start)
    return None


def _as_utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(to_utc_iso(value))
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def metric_coverage(rows: Sequence[Mapping], metric: str, anchor, range_key: str = DEFAULT_HISTORY_RANGE) -> dict:
    """Compare stored snapshots with the session windows the range should hold.

    ``expected`` is one timestamp per window, its left edge in UTC. ``actual``
    is the subset that has a snapshot. ``missing`` is the rest. ``null_metric``
    is a covered window whose value for this metric is null. Several stored
    rows in one window count as one bar; the row nearest the window start wins.
    """
    expected = expected_snapshot_timestamps(anchor, range_key)
    expected_set = set(expected)
    covered: dict[str, tuple[pd.Timestamp, Mapping]] = {}
    for row in rows:
        window = snapshot_window_start(row["scan_timestamp"])
        if window is None or window not in expected_set:
            continue
        stored_at = _as_utc(row["scan_timestamp"])
        window_at = _as_utc(window)
        distance = abs(stored_at - window_at)
        current = covered.get(window)
        if current is None or distance < current[0] or (distance == current[0] and stored_at >= _as_utc(current[1]["scan_timestamp"])):
            covered[window] = (distance, row)
    actual = [stamp for stamp in expected if stamp in covered]
    missing = [stamp for stamp in expected if stamp not in covered]
    null_metric = [
        stamp
        for stamp in actual
        if metric_display_value(covered[stamp][1], metric) is None
    ]
    return {
        "expected": expected,
        "actual": actual,
        "missing": missing,
        "null_metric": null_metric,
    }

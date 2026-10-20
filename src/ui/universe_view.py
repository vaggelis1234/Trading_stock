"""Display frames for the Universe v1 table, rank movement, and history."""

from datetime import timedelta
from typing import Mapping, Optional, Sequence

import pandas as pd

from src.market.calendar import NyseCalendar
from src.universe.metrics import to_utc_iso
from src.universe.schedule import FINAL, SLOT_GRACE, as_new_york, session_slots
from src.universe.ranking import TOP_N
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

DISPLAY_COLUMNS = [
    "Rank",
    "Movement",
    "Symbol",
    "Price",
    "Status",
    "Score",
    "RSI",
    "MACD Histogram",
    "ATR%",
    "Volume Ratio",
    "ADV",
]

CHART_COLUMNS = ["RSI", "Score", "ATR%", "Volume Ratio"]
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
DISPLAY_TIMEZONE = "Europe/Athens"


def _blank(value):
    if value is None or pd.isna(value):
        return pd.NA
    return value


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


def format_veto_reasons(status, reasons) -> str:
    """PASS is an em dash. BLOCKED reasons are joined in stored order."""
    if status == PASS:
        return "—"
    labels = [reason_text(code) for code in (reasons or [])]
    if not labels:
        return "—"
    return "; ".join(labels)


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


def format_score(value) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(number):
        return "—"
    return f"{number:.2f}"


def symbol_detail_lines(row: Mapping, previous_rank) -> list[str]:
    current = row.get("rank")
    if current is None:
        current_text = "—"
        movement = "—"
    else:
        current_text = str(int(current))
        _, movement = rank_movement(int(current), previous_rank)
    previous_text = "—" if previous_rank is None else str(int(previous_rank))
    status = display_status(row.get("veto_status"))
    lines = [
        f"Current rank: {current_text}",
        f"Previous rank: {previous_text}",
        f"Movement: {movement}",
        f"Score: {format_score(row.get('final_score'))}",
        f"Status: {status}",
    ]
    if status == "BLOCKED":
        reasons = [reason_text(code) for code in (row.get("veto_reasons") or [])]
        if reasons:
            lines.append("Reasons:")
            lines.extend(f"• {text}" for text in reasons)
    return lines


def _hour_floor(value) -> pd.Timestamp:
    """Clock hour that contains a snapshot. ``17:02`` belongs to ``17:00``."""
    return pd.Timestamp(to_utc_iso(value)).floor("h")


def history_start(anchor, range_key: str = DEFAULT_HISTORY_RANGE) -> str:
    if range_key not in HISTORY_RANGES:
        raise ValueError(f"unknown history range: {range_key}")
    return to_utc_iso(_hour_floor(anchor) - HISTORY_RANGES[range_key])


def as_display_timestamp(value) -> pd.Timestamp:
    """UTC storage value shown in Europe/Athens. Naive values are UTC."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.tz_convert(DISPLAY_TIMEZONE)


def format_local_timestamp(value) -> str:
    """Full market or execution timestamp in Europe/Athens."""
    if value is None or value == "":
        return "—"
    return as_display_timestamp(value).strftime("%Y-%m-%d %H:%M %Z")


def format_history_time(value, *, with_date: bool = False) -> str:
    stamp = as_display_timestamp(value)
    if with_date:
        return stamp.strftime("%m-%d %H:%M")
    return stamp.strftime("%H:%M")


def history_spans_dates(rows: Sequence[Mapping]) -> bool:
    dates = {as_display_timestamp(row["scan_timestamp"]).date() for row in rows}
    return len(dates) > 1


def selected_ranking_symbol(table: pd.DataFrame, selection) -> Optional[str]:
    """Symbol for a single-row ranking selection.

    An empty selection, or an index outside the table, uses the first row.
    """
    if table.empty or "Symbol" not in table.columns:
        return None
    rows = _selection_rows(selection)
    index = 0
    if rows:
        try:
            index = int(rows[0])
        except (TypeError, ValueError):
            index = 0
    if index < 0 or index >= len(table):
        index = 0
    symbol = table.iloc[index]["Symbol"]
    if symbol is None or pd.isna(symbol):
        return None
    return str(symbol)


def _selection_rows(selection) -> list:
    if selection is None:
        return []
    if isinstance(selection, Mapping):
        inner = selection.get("selection", selection)
    else:
        inner = getattr(selection, "selection", selection)
    if isinstance(inner, Mapping):
        rows = inner.get("rows") or []
    else:
        rows = getattr(inner, "rows", None) or []
    return list(rows)


def history_status_table(rows: Sequence[Mapping], *, with_date: Optional[bool] = None) -> pd.DataFrame:
    show_date = history_spans_dates(rows) if with_date is None else with_date
    columns = ["Time", "Status", "Reason"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        {
            "Time": [format_history_time(row["scan_timestamp"], with_date=show_date) for row in rows],
            "Status": [display_status(row.get("veto_status")) for row in rows],
            "Reason": [format_veto_reasons(row.get("veto_status"), row.get("veto_reasons")) for row in rows],
        }
    )


def _in_top(row: Mapping, limit: int) -> bool:
    rank = row.get("rank")
    if rank is None:
        return False
    try:
        number = int(rank)
    except (TypeError, ValueError):
        return False
    return 1 <= number <= limit


def latest_table(rows: Sequence[Mapping], previous_ranks: Optional[Mapping] = None, limit: int = TOP_N) -> pd.DataFrame:
    """Top scored symbols for the latest scan, in rank order.

    ``previous_ranks`` maps a symbol to its previous snapshot rank. A missing
    symbol, or a missing rank, is shown as NEW.
    """
    previous = {} if previous_ranks is None else previous_ranks
    visible = [row for row in rows if _in_top(row, limit)]
    visible.sort(key=lambda row: (int(row["rank"]), str(row.get("symbol", ""))))
    frame = pd.DataFrame(
        {
            "Rank": [int(row["rank"]) for row in visible],
            "Movement": [rank_movement(int(row["rank"]), previous.get(row.get("symbol")))[1] for row in visible],
            "Symbol": [row.get("symbol") for row in visible],
            "Price": [_blank(row.get("hourly_close")) for row in visible],
            "Status": [display_status(row.get("veto_status")) for row in visible],
            "Score": [_blank(row.get("final_score")) for row in visible],
            "RSI": [_blank(row.get("rsi14")) for row in visible],
            "MACD Histogram": [_blank(row.get("macd_hist")) for row in visible],
            "ATR%": [
                pd.NA if row.get("atr_percent") is None else float(row["atr_percent"]) * 100.0
                for row in visible
            ],
            "Volume Ratio": [_blank(row.get("volume_ratio")) for row in visible],
            "ADV": [_blank(row.get("adv20")) for row in visible],
        }
    )
    return frame.loc[:, DISPLAY_COLUMNS]


def format_snapshot_clock(value) -> str:
    """Compact Europe/Athens clock for a stored UTC snapshot timestamp."""
    return as_display_timestamp(value).strftime("%d/%m %H:%M")


def metric_display_value(row: Mapping, metric: str):
    """Chart-scale value for one stored snapshot. Null stays null.

    ATR% is percent points, matching the history chart. Nothing is filled in.
    """
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


def format_metric_value(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.2f}"


def metric_snapshot_options(rows: Sequence[Mapping], metric: str) -> list[str]:
    """One dropdown label per stored snapshot, in snapshot order.

    A null metric is shown as an em dash. Hours with no snapshot are omitted.
    """
    return [
        f"{format_snapshot_clock(row['scan_timestamp'])} — {format_metric_value(metric_display_value(row, metric))}"
        for row in rows
    ]


def expected_snapshot_timestamps(anchor, range_key: str = DEFAULT_HISTORY_RANGE) -> list[str]:
    """Session-window starts that should already be stored, oldest first.

    A regular cash day is six 60-minute windows from 09:30 and one closing
    half hour, 15:30–16:00, America/New_York. Weekends, holidays, and windows
    whose scan is still after ``anchor`` are not expected. An early close
    keeps only the windows that finish by that close.
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
    """Session window a stored snapshot belongs to, or None outside the cash session.

    A print inside 09:30–10:30 belongs to that window. A scan clock within
    five minutes after 10:45 still belongs to 09:30–10:30, not to the next
    hour. The 16:00 print belongs to the closing half hour.
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


def format_metric_coverage_summary(coverage: Mapping, metric: str) -> str:
    return (
        f"Expected {len(coverage['expected'])} session bars · "
        f"present {len(coverage['actual'])} · "
        f"missing {len(coverage['missing'])} · "
        f"null {metric} {len(coverage['null_metric'])}"
    )


def format_coverage_timestamps(stamps: Sequence[str]) -> str:
    return ", ".join(format_snapshot_clock(stamp) for stamp in stamps)


def history_chart_frame(rows: Sequence[Mapping]) -> pd.DataFrame:
    """Chart frame indexed by snapshot time. ATR% is shown in percent points."""
    if not rows:
        return pd.DataFrame(columns=CHART_COLUMNS)
    frame = pd.DataFrame(
        {
            "RSI": [row.get("rsi14") for row in rows],
            "Score": [row.get("final_score") for row in rows],
            "ATR%": [
                None if row.get("atr_percent") is None else float(row["atr_percent"]) * 100.0
                for row in rows
            ],
            "Volume Ratio": [row.get("volume_ratio") for row in rows],
        }
    )
    frame.index = pd.DatetimeIndex([as_display_timestamp(row["scan_timestamp"]) for row in rows])
    frame.index.name = "scan_timestamp"
    return frame.loc[:, CHART_COLUMNS]

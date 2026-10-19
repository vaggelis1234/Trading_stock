"""Run one scheduled Universe slot through the existing score and rank path.

The hourly frame is the reconstructed session windows only. Scoring,
ranking, and the hard veto are unchanged. A window that is missing or still
inside the SIP delay does not become a bar, and the slot is not a successful
snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from src.data.provider import prepare_ohlcv
from src.data.scan_windows import reconstruct_scheduled_bars
from src.market.calendar import NyseCalendar
from src.universe.pipeline import run_universe
from src.universe.schedule import ScanSlot, as_new_york

logger = logging.getLogger(__name__)


@dataclass
class ScheduledScanResult:
    ok: bool
    rows: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    slot_id: str = ""


def run_scheduled_universe(
    symbols: list[str],
    slot: ScanSlot,
    *,
    calendar: NyseCalendar | None = None,
    provider=None,
    bars_30m: dict | None = None,
    daily_frames: dict | None = None,
    db_path: str = "artifacts/universe.db",
    bars_db: str | None = None,
    artifact_dir: str = "artifacts",
    lookback_bars: int = 24,
    hourly_lookback_days: int = 60,
    now: pd.Timestamp | None = None,
    weights_path: str | None = None,
    earnings=None,
) -> ScheduledScanResult:
    """Score ``slot`` from completed 30-minute bars. Does not submit orders."""
    clock = as_new_york(slot.scan_at if now is None else now)
    sessions = (calendar or NyseCalendar()).recent_sessions(slot.session_date, hourly_lookback_days)
    from src.universe.schedule import session_slots

    history = [item for session in sessions for item in session_slots(session)]
    if bars_30m is None:
        bars_30m = _fetch_30m(provider, symbols, sessions, clock)
    hourly_frames = {}
    reasons = []
    for symbol in symbols:
        built = reconstruct_scheduled_bars(bars_30m.get(symbol), history, clock)
        hourly_frames[symbol] = built.frame()
        for rejection in built.rejections:
            if rejection.window_start == slot.window_start.tz_convert("UTC"):
                reasons.append({"symbol": symbol, "reason": rejection.reason})
                logger.warning(
                    "scheduled window %s %s rejected for %s",
                    slot.slot_id,
                    rejection.reason,
                    symbol,
                )
    ready = any(_contains(hourly_frames[symbol], slot) for symbol in symbols)
    if not ready:
        logger.warning(
            "scheduled scan %s has no completed window; snapshot was not written",
            slot.slot_id,
        )
        return ScheduledScanResult(ok=False, reasons=reasons, slot_id=slot.slot_id)
    rows = run_universe(
        symbols,
        provider=provider,
        earnings=earnings,
        db_path=db_path,
        bars_db=bars_db,
        artifact_dir=artifact_dir,
        lookback_bars=lookback_bars,
        weights_path=weights_path,
        now=clock,
        hourly_frames=hourly_frames,
        daily_frames=daily_frames,
        require_bar_at=slot.window_start,
    )
    return ScheduledScanResult(ok=True, rows=rows, reasons=reasons, slot_id=slot.slot_id)


def _contains(frame: pd.DataFrame, slot: ScanSlot) -> bool:
    prepared = prepare_ohlcv(frame)
    if prepared.empty:
        return False
    return slot.window_start.tz_convert("UTC").floor("min") in prepared.index


def _fetch_30m(provider, symbols: list[str], sessions, clock: pd.Timestamp) -> dict:
    if provider is None or not sessions:
        return {symbol: pd.DataFrame() for symbol in symbols}
    start = sessions[0].day.isoformat()
    end = (clock + pd.Timedelta(minutes=1)).tz_convert("UTC").isoformat()
    batch = provider.get_bars_many(symbols, start, end, "30m")
    frames = {symbol: batch.frames.get(symbol, pd.DataFrame()) for symbol in symbols}
    for symbol in batch.failed:
        logger.warning("30-minute SIP fetch failed for %s", symbol)
        frames[symbol] = pd.DataFrame()
    return frames

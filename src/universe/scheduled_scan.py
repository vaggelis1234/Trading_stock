"""Run one scheduled Universe slot through the existing score and rank path.

The intraday frame is completed regular-session Alpaca ``30Min`` bars.
Scoring, ranking, and the hard veto are unchanged. A window that is missing
or still inside the SIP delay does not become a bar, and the slot is not a
successful snapshot.
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
    lookback_bars: int = 48,
    hourly_lookback_days: int = 60,
    now: pd.Timestamp | None = None,
    weights_path: str | None = None,
    earnings=None,
    stop_check=None,
    progress=None,
) -> ScheduledScanResult:
    """Score ``slot`` from completed 30-minute bars. Does not submit orders."""
    def _relay(percent: int) -> None:
        if progress is not None:
            progress(percent)

    clock = as_new_york(slot.scan_at if now is None else now)
    sessions = (calendar or NyseCalendar()).recent_sessions(slot.session_date, hourly_lookback_days)
    from src.universe.schedule import session_slots

    history = [item for session in sessions for item in session_slots(session)]
    if bars_30m is None:
        synced = {"count": 0}
        symbol_count = max(len(symbols), 1)

        def _on_sync(count: int) -> None:
            synced["count"] += count
            _relay(min(70, int(synced["count"] * 70 / symbol_count)))

        bars_30m = _load_cached_30m(
            provider, symbols, sessions, clock, bars_db, stop_check=stop_check, on_item=_on_sync,
        )
        def score_progress(percent: int) -> None:
            _relay(min(100, 70 + int(percent * 30 / 100)))
    else:
        score_progress = _relay
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
        stop_check=stop_check,
        progress=score_progress,
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


def _load_cached_30m(provider, symbols: list[str], sessions, clock: pd.Timestamp, bars_db: str | None, stop_check=None, on_item=None) -> dict:
    """Sync ``30m`` through the bar cache, then return the stored window.

    The first scan backfills the lookback. A later scan refreshes the cache
    tail instead of requesting the whole lookback again.
    """
    if provider is None or not sessions:
        return {symbol: pd.DataFrame() for symbol in symbols}
    from src.data.bar_store import BarStore, BarSyncStopped
    from src.universe.pipeline import UniverseScanStopped

    start = sessions[0].day.isoformat()
    end = (clock + pd.Timedelta(minutes=1)).tz_convert("UTC").isoformat()
    store = BarStore(bars_db)
    try:
        store.sync_many(provider, symbols, "30m", start, end, clock, stop_check=stop_check, on_item=on_item)
    except BarSyncStopped as exc:
        raise UniverseScanStopped("universe scan stopped") from exc
    return {symbol: store.load_window(symbol, "30m", start, end) for symbol in symbols}

"""Universe v1 scan: data, metrics, hard veto, score, rank, persist.

The scan does not submit orders. Each hourly bar in the lookback window
becomes its own snapshot. A later scan inserts new timestamps and leaves
existing rows in place.
"""

from collections import defaultdict
import logging
import os
from typing import Optional

import pandas as pd

from src.data.bar_store import BarStore, bars_db_path
from src.data.minute_enrichment import enrich_candidates, save_minute_enrichment
from src.data.provider import YFinanceMarketDataProvider, prepare_ohlcv
from src.persistence.store import save_universe_snapshots
from src.universe.earnings import UnavailableEarningsProvider, earnings_within_48h
from src.universe.metrics import compute_symbol_metrics, metrics_as_of, precompute_symbol_metrics, to_utc_iso
from src.universe.minute_candidates import max_candidates_for_1m, select_minute_candidates
from src.universe.ranking import rank_snapshots
from src.universe.scoring import apply_score, load_universe_weights
from src.universe.veto import apply_hard_vetoes

logger = logging.getLogger(__name__)

EMPTY_SCORES = {
    "trend_score": None,
    "momentum_score": None,
    "volatility_score": None,
    "volume_liquidity_score": None,
    "final_score": None,
    "rank": None,
}


def common_timestamps(hourly_by_symbol: dict, lookback: int) -> list[pd.Timestamp]:
    """Shared hourly clock, then the last ``lookback`` bars.

    The intersection is preferred so a ranking at time T uses symbols observed
    at T. If the clocks do not overlap, the union is used instead.
    """
    indexes = [pd.DatetimeIndex(frame.index) for frame in hourly_by_symbol.values() if frame is not None and len(frame)]
    if not indexes:
        return []
    common = indexes[0]
    for index in indexes[1:]:
        common = common.intersection(index)
    if len(common) == 0:
        clock = indexes[0]
        for index in indexes[1:]:
            clock = clock.union(index)
    else:
        clock = common
    clock = clock.sort_values()
    if lookback > 0:
        clock = clock[-lookback:]
    return list(clock)


def _scored_row(symbol: str, as_of, metrics: dict, earnings_flag: Optional[bool], weights: dict) -> dict:
    """Veto and score for metrics already sampled at ``as_of``."""
    veto = apply_hard_vetoes(
        daily_close=metrics["daily_close"],
        daily_ema200=metrics["daily_ema200"],
        rsi14=metrics["rsi14"],
        atr_percent=metrics["atr_percent"],
        volume_ratio=metrics["volume_ratio"],
        adv20=metrics["adv20"],
        earnings_within_48h=earnings_flag,
    )
    row = {
        "scan_timestamp": to_utc_iso(as_of),
        "symbol": symbol,
        "hourly_close": metrics["hourly_close"],
        "daily_close": metrics["daily_close"],
        "daily_ema200": metrics["daily_ema200"],
        "rsi14": metrics["rsi14"],
        "macd_hist": metrics["macd_hist"],
        "atr14": metrics["atr14"],
        "atr_percent": metrics["atr_percent"],
        "volume": metrics["volume"],
        "volume_ratio": metrics["volume_ratio"],
        "adv20": metrics["adv20"],
        "veto_status": veto.status,
        "veto_reasons": list(veto.reasons),
        "earnings_check": veto.earnings_check,
        **EMPTY_SCORES,
    }
    return apply_score(row, weights)


def evaluate_symbol(symbol: str, daily: pd.DataFrame, hourly: pd.DataFrame, as_of, earnings_flag: Optional[bool], weights: dict) -> dict:
    """Metrics, veto, and score for one symbol at one timestamp."""
    return _scored_row(symbol, as_of, compute_symbol_metrics(daily, hourly, as_of), earnings_flag, weights)


def _insufficient_row(symbol: str, as_of, earnings_flag: Optional[bool]) -> dict:
    veto = apply_hard_vetoes(
        daily_close=None,
        daily_ema200=None,
        rsi14=None,
        atr_percent=None,
        volume_ratio=None,
        adv20=None,
        earnings_within_48h=earnings_flag,
    )
    return {
        "scan_timestamp": to_utc_iso(as_of),
        "symbol": symbol,
        "hourly_close": None,
        "daily_close": None,
        "daily_ema200": None,
        "rsi14": None,
        "macd_hist": None,
        "atr14": None,
        "atr_percent": None,
        "volume": None,
        "volume_ratio": None,
        "adv20": None,
        "veto_status": veto.status,
        "veto_reasons": list(veto.reasons),
        "earnings_check": veto.earnings_check,
        **EMPTY_SCORES,
    }


def _export_latest(rows: list[dict], artifact_dir: str) -> None:
    if not rows:
        return
    os.makedirs(artifact_dir, exist_ok=True)
    stamp = pd.Timestamp(rows[0]["scan_timestamp"]).strftime("%Y%m%dT%H%M%SZ")
    frame = pd.DataFrame(rows)
    frame["veto_reasons"] = frame["veto_reasons"].apply(lambda reasons: ",".join(reasons or []))
    frame.to_csv(os.path.join(artifact_dir, f"universe_{stamp}.csv"), index=False)
    scores = pd.DataFrame(
        {
            "symbol": [row["symbol"] for row in rows],
            "price": [row["hourly_close"] for row in rows],
            "score": [row["final_score"] for row in rows],
            "veto_status": [row["veto_status"] for row in rows],
            "rank": [row["rank"] for row in rows],
        }
    )
    scores.to_csv(os.path.join(artifact_dir, f"scores_{stamp}.csv"), index=False)


def run_universe(
    symbols: list[str],
    *,
    provider=None,
    earnings=None,
    db_path: str = "artifacts/universe.db",
    bars_db: Optional[str] = None,
    artifact_dir: str = "artifacts",
    lookback_bars: int = 24,
    daily_lookback_days: int = 520,
    hourly_lookback_days: int = 60,
    weights_path: Optional[str] = None,
    now: Optional[pd.Timestamp] = None,
) -> list[dict]:
    """Evaluate ``symbols`` and append snapshots. Returns the latest ranked scan."""
    provider = provider or YFinanceMarketDataProvider()
    earnings = earnings if earnings is not None else UnavailableEarningsProvider()
    weights = load_universe_weights(weights_path)
    minute_limit = max_candidates_for_1m()

    clock = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if clock.tzinfo is None:
        clock = clock.tz_localize("UTC")
    end = clock + pd.Timedelta(days=1)
    daily_start = (end - pd.Timedelta(days=daily_lookback_days)).strftime("%Y-%m-%d")
    hourly_start = (end - pd.Timedelta(days=hourly_lookback_days)).strftime("%Y-%m-%d")
    end_text = end.strftime("%Y-%m-%d")

    bars = BarStore(bars_db_path(bars_db))
    daily_map = {}
    hourly_map = {}
    for symbol in symbols:
        try:
            daily_map[symbol] = bars.sync_and_load(provider, symbol, "1d", daily_start, end_text, clock)
            hourly_map[symbol] = bars.sync_and_load(provider, symbol, "1h", hourly_start, end_text, clock)
        except Exception:
            logger.exception("market data sync failed for %s", symbol)
            daily_map[symbol] = prepare_ohlcv(None)
            hourly_map[symbol] = prepare_ohlcv(None)
    logger.info(
        "bar sync initialized=%s backfilled=%s refreshed=%s skipped=%s failed=%s rebuilt=%s",
        bars.actions["initialized"],
        bars.actions["backfilled"],
        bars.actions["refreshed"],
        bars.actions["skipped"],
        bars.actions["failed"],
        bars.actions["rebuilt"],
    )

    timestamps = common_timestamps(hourly_map, lookback_bars)
    if not timestamps:
        timestamps = [clock.floor("min")]

    grouped: dict = defaultdict(list)
    for symbol in symbols:
        hourly = hourly_map[symbol]
        daily = daily_map[symbol]
        symbol_times = timestamps if len(hourly) else [timestamps[-1]]
        prepared = None
        if len(hourly) or len(daily):
            prepared = precompute_symbol_metrics(daily, hourly)
        for as_of in symbol_times:
            try:
                flag = earnings_within_48h(earnings, symbol, as_of)
            except Exception:
                logger.exception("earnings lookup failed for %s", symbol)
                flag = None
            if prepared is None:
                grouped[as_of].append(_insufficient_row(symbol, as_of, flag))
                continue
            grouped[as_of].append(_scored_row(symbol, as_of, metrics_as_of(prepared, as_of), flag, weights))

    latest: list[dict] = []
    latest_candidates: list[dict] = []
    for as_of in timestamps:
        ranked = rank_snapshots(grouped.get(as_of, []))
        if not ranked:
            continue
        candidates = select_minute_candidates(ranked, limit=minute_limit)
        save_universe_snapshots(ranked, db_path)
        latest = ranked
        latest_candidates = candidates
        passed = sum(1 for row in ranked if row["veto_status"] == "PASS")
        scored = sum(1 for row in ranked if row.get("final_score") is not None)
        logger.info(
            "universe snapshot %s pass=%s blocked=%s scored=%s minute_candidates=%s",
            ranked[0]["scan_timestamp"],
            passed,
            len(ranked) - passed,
            scored,
            [row["symbol"] for row in candidates],
        )

    _export_latest(latest, artifact_dir)
    if latest_candidates:
        enrichment = enrich_candidates(provider, latest_candidates, hourly_map, bars)
        save_minute_enrichment(db_path, enrichment)
    return latest

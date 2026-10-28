"""Compare legacy hourly Universe metrics with 30-minute metrics.

The score formulas and veto thresholds stay as they are. This script only
reports how those unchanged rules treat the two bar series. It does not
submit orders and it does not rewrite stored snapshots.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.data.provider import prepare_ohlcv
from src.data.scan_windows import reconstruct_scheduled_bars
from src.features.indicators import atr, avg_dollar_volume, ema, macd_hist, rsi, volume_ratio
from src.market.calendar import NyseCalendar
from src.universe.metrics import ADV_WINDOW, EMA_SPAN, HOURLY_STALE_AFTER, as_utc, completed_daily
from src.universe.schedule import legacy_hourly_slots, session_slots
from src.universe.scoring import apply_score, load_universe_weights
from src.universe.veto import ATR_PERCENT_MAX, apply_hard_vetoes

LEGACY_WINDOWS = {"rsi": 14, "atr": 14, "volume": 20, "macd": (12, 26, 9)}
HALF_HOUR_WINDOWS = {"rsi": 28, "atr": 28, "volume": 40, "macd": (24, 52, 18)}


def _slots(day, builder, calendar, lookback_days):
    sessions = calendar.recent_sessions(day, lookback_days)
    return [slot for session in sessions for slot in builder(session)]


def series_at(bars_30m, daily, as_of, day, builder, calendar, windows, lookback_days=80):
    built = reconstruct_scheduled_bars(bars_30m, _slots(day, builder, calendar, lookback_days), as_of)
    metrics = _metrics(daily, built.frame(), as_of, windows)
    veto = apply_hard_vetoes(
        daily_close=metrics["daily_close"],
        daily_ema200=metrics["daily_ema200"],
        rsi14=metrics["rsi14"],
        atr_percent=metrics["atr_percent"],
        volume_ratio=metrics["volume_ratio"],
        adv20=metrics["adv20"],
        earnings_within_48h=None,
    )
    scored = apply_score({**metrics, "veto_status": veto.status}, load_universe_weights())
    return {
        "status": veto.status,
        "reasons": list(veto.reasons),
        "rsi14": metrics["rsi14"],
        "macd_over_price": _ratio(metrics["macd_hist"], metrics["hourly_close"]),
        "atr_percent": metrics["atr_percent"],
        "volume_ratio": metrics["volume_ratio"],
        "final_score": scored["final_score"],
        "atr_above_4pct": metrics["atr_percent"] is not None and metrics["atr_percent"] > ATR_PERCENT_MAX,
    }


def compare_symbol(symbol, bars_30m, daily, as_of, day, calendar=None):
    calendar = calendar or NyseCalendar()
    legacy = series_at(bars_30m, daily, as_of, day, legacy_hourly_slots, calendar, LEGACY_WINDOWS)
    current = series_at(bars_30m, daily, as_of, day, session_slots, calendar, HALF_HOUR_WINDOWS)
    return {"symbol": symbol, "legacy_hourly": legacy, "half_hour": current}


def summarize(rows):
    fields = ("rsi14", "macd_over_price", "atr_percent", "volume_ratio", "final_score")
    summary = {"symbols": len(rows), "status_changes": [], "fields": {}}
    for field in fields:
        legacy = [row["legacy_hourly"][field] for row in rows if row["legacy_hourly"][field] is not None]
        current = [row["half_hour"][field] for row in rows if row["half_hour"][field] is not None]
        ratios = [
            row["half_hour"][field] / row["legacy_hourly"][field]
            for row in rows
            if row["legacy_hourly"][field] not in (None, 0) and row["half_hour"][field] is not None
        ]
        summary["fields"][field] = {
            "legacy_median": _median(legacy),
            "half_hour_median": _median(current),
            "half_over_legacy_median": _median(ratios),
        }
    for row in rows:
        if row["legacy_hourly"]["status"] != row["half_hour"]["status"]:
            summary["status_changes"].append(
                {
                    "symbol": row["symbol"],
                    "legacy": row["legacy_hourly"]["status"],
                    "half_hour": row["half_hour"]["status"],
                    "legacy_atr_percent": row["legacy_hourly"]["atr_percent"],
                    "half_hour_atr_percent": row["half_hour"]["atr_percent"],
                }
            )
    summary["legacy_atr_above_4pct"] = sum(row["legacy_hourly"]["atr_above_4pct"] for row in rows)
    summary["half_hour_atr_above_4pct"] = sum(row["half_hour"]["atr_above_4pct"] for row in rows)
    summary["threshold_atr_percent"] = ATR_PERCENT_MAX
    return summary


def _metrics(daily, intraday, as_of, windows):
    """Causal metrics with an explicit indicator window. Thresholds are not applied here."""
    as_of_ts = as_utc(as_of).floor("min")
    metrics = {
        "hourly_close": None,
        "daily_close": None,
        "daily_ema200": None,
        "rsi14": None,
        "macd_hist": None,
        "atr_percent": None,
        "volume_ratio": None,
        "adv20": None,
    }
    frame = prepare_ohlcv(intraday)
    if not frame.empty:
        position = int(frame.index.searchsorted(as_of_ts, side="right")) - 1
        if position >= 0 and as_of_ts - frame.index[position] <= HOURLY_STALE_AFTER:
            close = float(frame["close"].iloc[position])
            metrics["hourly_close"] = close
            metrics["rsi14"] = float(rsi(frame, windows["rsi"]).iloc[position])
            fast, slow, signal = windows["macd"]
            metrics["macd_hist"] = float(macd_hist(frame, fast, slow, signal).iloc[position])
            atr_value = float(atr(frame, windows["atr"]).iloc[position])
            if close:
                metrics["atr_percent"] = atr_value / close
            metrics["volume_ratio"] = float(volume_ratio(frame, windows["volume"]).iloc[position])
    daily_frame = completed_daily(daily, as_of_ts)
    if not daily_frame.empty:
        metrics["daily_close"] = float(daily_frame["close"].iloc[-1])
        if len(daily_frame) >= EMA_SPAN:
            metrics["daily_ema200"] = float(ema(daily_frame["close"], EMA_SPAN).iloc[-1])
        if len(daily_frame) >= ADV_WINDOW:
            metrics["adv20"] = float(avg_dollar_volume(daily_frame, ADV_WINDOW).iloc[-1])
    return metrics


def _ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def _median(values):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _finite_bar(frame):
    if frame is None or frame.empty:
        return frame
    return frame.replace([math.inf, -math.inf], pd.NA).dropna()


def verify_session_edges(symbol, bars_30m, minutes, day):
    """Compare the 09:30 and 15:30 SIP bars with the 1-minute bars inside them.

    Nothing is subtracted from either series. A difference is reported as-is.
    """
    report = []
    for label in ("09:30", "15:30", "16:00"):
        edge = pd.Timestamp(f"{day.isoformat()} {label}", tz="America/New_York").tz_convert("UTC")
        end = edge + pd.Timedelta(minutes=30)
        bar = _row_at(bars_30m, edge)
        window = _between(minutes, edge, end)
        minute_volume = None if window is None or window.empty else float(window["volume"].sum())
        minute_close = None if window is None or window.empty else float(window["close"].iloc[-1])
        report.append(
            {
                "symbol": symbol,
                "label": label,
                "bar_present": bar is not None,
                "bar_open": None if bar is None else float(bar["open"]),
                "bar_high": None if bar is None else float(bar["high"]),
                "bar_low": None if bar is None else float(bar["low"]),
                "bar_close": None if bar is None else float(bar["close"]),
                "bar_volume": None if bar is None else float(bar["volume"]),
                "minute_count": 0 if window is None else int(len(window)),
                "minute_volume_sum": minute_volume,
                "minute_last_close": minute_close,
                "volume_delta": None if bar is None or minute_volume is None else float(bar["volume"]) - minute_volume,
                "close_delta": None if bar is None or minute_close is None else float(bar["close"]) - minute_close,
            }
        )
    return report


def _row_at(frame, edge):
    if frame is None or frame.empty:
        return None
    prepared = frame.copy()
    prepared.index = pd.DatetimeIndex(pd.to_datetime(prepared.index, utc=True)).floor("min")
    stamp = edge.floor("min")
    if stamp not in prepared.index:
        return None
    return prepared.loc[stamp]


def _between(frame, start, end):
    if frame is None or frame.empty:
        return frame
    prepared = frame.copy()
    prepared.index = pd.DatetimeIndex(pd.to_datetime(prepared.index, utc=True))
    return prepared.loc[(prepared.index >= start) & (prepared.index < end)].sort_index()


def fetch_live(symbols, day):
    from src.data.provider import AlpacaMarketDataProvider

    provider = AlpacaMarketDataProvider(request_delay=0)
    start = (pd.Timestamp(day) - pd.Timedelta(days=80)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    intraday = provider.get_bars_many(symbols, start, end, "30m")
    daily = provider.get_bars_many(symbols, "2024-01-01", end, "1d")
    minute_start = pd.Timestamp(f"{day.isoformat()} 09:00", tz="America/New_York").tz_convert("UTC")
    minute_end = pd.Timestamp(f"{day.isoformat()} 16:40", tz="America/New_York").tz_convert("UTC")
    minutes = {
        symbol: provider.get_bars(symbol, minute_start.isoformat(), minute_end.isoformat(), "1m")
        for symbol in symbols
    }
    return intraday, daily, minutes


def main():
    symbols = [part.strip().upper() for part in os.getenv("COMPARE_SYMBOLS", "AAPL,MSFT,NVDA").split(",") if part.strip()]
    day = pd.Timestamp(os.getenv("COMPARE_DAY", "2026-10-20")).date()
    as_of = pd.Timestamp(f"{day.isoformat()} 16:15", tz="America/New_York")
    intraday, daily, minutes = fetch_live(symbols, day)
    calendar = NyseCalendar()
    compared = []
    edges = []
    for symbol in symbols:
        bars = _finite_bar(intraday.frames.get(symbol))
        daily_frame = _finite_bar(daily.frames.get(symbol))
        compared.append(compare_symbol(symbol, bars, daily_frame, as_of, day, calendar))
        edges.extend(verify_session_edges(symbol, bars, minutes.get(symbol), day))
    payload = {"day": day.isoformat(), "as_of": as_of.isoformat(), "summary": summarize(compared), "rows": compared, "edges": edges}
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()

"""A/B Yahoo vs Alpaca historical SIP for five Universe symbols.

This script does not select the production provider and does not open
``market_bars.db``. Each provider is scored only from its own bars.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.provider import AlpacaMarketDataProvider, YFinanceMarketDataProvider
from src.universe.metrics import HOURLY_STALE_AFTER, as_utc, metrics_as_of, precompute_symbol_metrics
from src.universe.pipeline import evaluate_symbol
from src.universe.ranking import TOP_N, rank_snapshots
from src.universe.scoring import load_universe_weights

SYMBOLS = ["AAPL", "MSFT", "BRK-B", "NVDA", "POOL"]
DAILY_LOOKBACK_DAYS = 520
HOURLY_LOOKBACK_DAYS = 60
DAILY_COMPARE_SESSIONS = 20
HOURLY_COMPARE_SESSIONS = 5
SIP_LAG = pd.Timedelta(minutes=20)
OHLCV = ["open", "high", "low", "close", "volume"]
PRICE_FIELDS = ["open", "high", "low", "close"]
INDICATORS = [
    ("adv20", "ADV20"),
    ("volume_ratio", "VolumeRatio"),
    ("rsi14", "RSI"),
    ("macd_hist", "MACD"),
    ("atr14", "ATR"),
    ("daily_ema200", "EMA200"),
]
SCORE_PARTS = (
    "trend_score",
    "momentum_score",
    "volatility_score",
    "volume_liquidity_score",
    "final_score",
)


def output_dir() -> Path:
    path = (ROOT / "artifacts" / "ab_alpaca_sip").resolve()
    if "market_bars.db" in path.parts or path.name == "market_bars.db":
        raise RuntimeError("A/B output must not use market_bars.db")
    path.mkdir(parents=True, exist_ok=True)
    return path


def in_expected_session(stamp, interval: str) -> bool:
    """Regular-session membership for a bar timestamp.

    Daily bars are labeled at New York midnight, so a weekday label is the
    session. Hourly bars count only when the label falls in
    [09:30, 16:00) America/New_York on a weekday. Overnight hours, weekends,
    and the 16:00 close label are outside that window.
    """
    et = as_utc(stamp).tz_convert("America/New_York")
    if et.weekday() >= 5:
        return False
    if interval == "1d":
        return True
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def _iso(stamp) -> str:
    return as_utc(stamp).isoformat()


def _neighbor(index: pd.DatetimeIndex, stamp: pd.Timestamp, side: str):
    if index is None or len(index) == 0:
        return None
    if side == "prev":
        earlier = index[index < stamp]
        return _iso(earlier[-1]) if len(earlier) else None
    later = index[index > stamp]
    return _iso(later[0]) if len(later) else None


def align_frames(symbol: str, interval: str, yahoo: pd.DataFrame, alpaca: pd.DataFrame) -> list[dict]:
    """Union of real timestamps. No filled, resampled, or invented bars."""
    yahoo_index = pd.DatetimeIndex(yahoo.index) if yahoo is not None and len(yahoo) else pd.DatetimeIndex([])
    alpaca_index = pd.DatetimeIndex(alpaca.index) if alpaca is not None and len(alpaca) else pd.DatetimeIndex([])
    if yahoo_index.tz is None and len(yahoo_index):
        yahoo_index = yahoo_index.tz_localize("UTC")
    if alpaca_index.tz is None and len(alpaca_index):
        alpaca_index = alpaca_index.tz_localize("UTC")
    stamps = yahoo_index.union(alpaca_index).sort_values()
    rows = []
    for stamp in stamps:
        in_yahoo = stamp in yahoo_index
        in_alpaca = stamp in alpaca_index
        if in_yahoo and in_alpaca:
            presence = "BOTH_PRESENT"
        elif in_yahoo:
            presence = "YAHOO_ONLY"
        else:
            presence = "ALPACA_ONLY"
        row = {
            "symbol": symbol,
            "interval": interval,
            "timestamp": _iso(stamp),
            "presence": presence,
            "in_expected_session": in_expected_session(stamp, interval),
            "yahoo_prev": _neighbor(yahoo_index, stamp, "prev"),
            "yahoo_next": _neighbor(yahoo_index, stamp, "next"),
            "alpaca_prev": _neighbor(alpaca_index, stamp, "prev"),
            "alpaca_next": _neighbor(alpaca_index, stamp, "next"),
        }
        for field in OHLCV:
            row[f"yahoo_{field}"] = _cell(yahoo, stamp, field) if in_yahoo else None
            row[f"alpaca_{field}"] = _cell(alpaca, stamp, field) if in_alpaca else None
        rows.append(row)
    return rows


def _cell(frame: pd.DataFrame, stamp: pd.Timestamp, field: str):
    value = frame.at[stamp, field]
    if pd.isna(value):
        return None
    return float(value)


def _pct(baseline, other):
    if baseline is None or other is None:
        return None
    if baseline == 0:
        return 0.0 if other == 0 else None
    return abs(other - baseline) / abs(baseline) * 100.0


def _summary_stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None}
    series = pd.Series(values, dtype=float)
    return {
        "n": int(len(series)),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "p95": float(series.quantile(0.95)),
    }


def session_dates(frame: pd.DataFrame) -> list:
    if frame is None or frame.empty:
        return []
    et = frame.index.tz_convert("America/New_York")
    return sorted({stamp.date() for stamp in et if stamp.weekday() < 5})


def _in_dates(frame: pd.DataFrame, dates: set) -> pd.DataFrame:
    if frame is None or frame.empty or not dates:
        return frame.iloc[0:0] if frame is not None else frame
    mask = [stamp.tz_convert("America/New_York").date() in dates for stamp in frame.index]
    return frame.loc[mask]


def _save_frame(path: Path, frame: pd.DataFrame) -> None:
    if "market_bars.db" in path.parts:
        raise RuntimeError("A/B output must not use market_bars.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out.index.name = "timestamp"
    out.to_csv(path)


def _candidate(rows: list[dict]):
    passing = [row for row in rows if row.get("veto_status") == "PASS" and row.get("rank") is not None]
    if not passing:
        return None
    passing.sort(key=lambda row: (row["rank"], row["symbol"]))
    return passing[0]["symbol"]


def _top_set(rows: list[dict]) -> set[str]:
    return {row["symbol"] for row in rows if row.get("rank") is not None and row["rank"] <= TOP_N}


def _spearman(left: list[float], right: list[float]):
    if len(left) < 2 or len(left) != len(right):
        return None
    a = pd.Series(left, dtype=float)
    b = pd.Series(right, dtype=float)
    if a.nunique(dropna=True) < 2 or b.nunique(dropna=True) < 2:
        return 1.0 if list(a) == list(b) else None
    value = a.rank().corr(b.rank())
    if pd.isna(value):
        return None
    return float(value)


def _own_hourly_bar(hourly: pd.DataFrame, as_of):
    if hourly is None or hourly.empty:
        return None
    as_of_ts = as_utc(as_of).floor("min")
    position = int(hourly.index.searchsorted(as_of_ts, side="right")) - 1
    if position < 0:
        return None
    stamp = hourly.index[position]
    if as_of_ts - stamp > HOURLY_STALE_AFTER:
        return None
    return stamp


def _classify_case(score_delta, pass_same: bool, candidate_same: bool, rank_delta) -> str:
    if not pass_same or not candidate_same:
        return "DECISION-CHANGING"
    if rank_delta not in (None, 0) or (score_delta is not None and abs(score_delta) >= 0.50):
        return "MATERIAL"
    if score_delta is not None and abs(score_delta) >= 0.10:
        return "SMALL"
    return "IDENTICAL / NEGLIGIBLE"


def _affected_indicator(yahoo_row: dict, alpaca_row: dict) -> str:
    best_name = ""
    best_delta = -1.0
    for key, label in INDICATORS:
        left = yahoo_row.get(key)
        right = alpaca_row.get(key)
        if left is None or right is None or left == 0:
            continue
        delta = abs(right - left) / abs(left)
        if delta > best_delta:
            best_delta = delta
            best_name = label
    part = ""
    part_delta = -1.0
    for key in SCORE_PARTS:
        left = yahoo_row.get(key)
        right = alpaca_row.get(key)
        if left is None or right is None:
            continue
        delta = abs(right - left)
        if delta > part_delta:
            part_delta = delta
            part = key
    if best_name and part:
        return f"{best_name} -> {part}"
    return best_name or part or "none"


def request_end(now=None) -> pd.Timestamp:
    """Exclusive end as a timezone-aware timestamp.

    Yahoo parses date strings with ``%Y-%m-%d`` only. A full ISO-8601 string
    such as ``2026-10-04T17:55:00+00:00`` raises ``unconverted data remains``.
    A ``Timestamp`` keeps the same instant for both providers.
    """
    clock = pd.Timestamp.now(tz="UTC") if now is None else as_utc(now)
    return (clock - SIP_LAG).floor("min")


def _verdict(summary: dict) -> str:
    if not summary["data_quality_pass"]:
        return "NEEDS MORE TESTING"
    if summary["in_session_one_sided_share"] > 0.25:
        return "NEEDS MORE TESTING"
    if (
        summary["price_pass"]
        and summary["volume_pass"]
        and summary["indicator_pass"]
        and summary["ranking_pass"]
        and summary["decision_pass"]
    ):
        return "ALPACA SIP ACCEPT"
    return "NEEDS MORE TESTING"


def _pass_price(stats: dict) -> bool:
    return stats["n"] > 0 and stats["median"] is not None and stats["median"] <= 0.25 and stats["p95"] <= 1.0


def _pass_volume(stats: dict) -> bool:
    return stats["n"] > 0 and stats["median"] is not None and stats["median"] <= 5.0 and stats["p95"] <= 15.0


def run() -> int:
    out = output_dir()
    end = request_end()
    end_text = end.isoformat()
    daily_start = (end - pd.Timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    hourly_start = (end - pd.Timedelta(days=HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    yahoo_provider = YFinanceMarketDataProvider()
    alpaca_provider = AlpacaMarketDataProvider()
    weights = load_universe_weights()

    store = {}
    errors = []
    for symbol in SYMBOLS:
        store[symbol] = {}
        for interval, start in (("1d", daily_start), ("1h", hourly_start)):
            try:
                yahoo = yahoo_provider.get_bars(symbol, start, end, interval)
                alpaca = alpaca_provider.get_bars(symbol, start, end, interval)
            except Exception as exc:
                errors.append({"symbol": symbol, "interval": interval, "error": str(exc)})
                yahoo = pd.DataFrame(columns=OHLCV)
                alpaca = pd.DataFrame(columns=OHLCV)
            store[symbol][interval] = {"yahoo": yahoo, "alpaca": alpaca}
            _save_frame(out / "yahoo" / f"{symbol}_{interval}.csv", yahoo)
            _save_frame(out / "alpaca" / f"{symbol}_{interval}.csv", alpaca)

    daily_dates = []
    hourly_dates = []
    for symbol in SYMBOLS:
        daily_dates.extend(session_dates(store[symbol]["1d"]["yahoo"]))
        daily_dates.extend(session_dates(store[symbol]["1d"]["alpaca"]))
        hourly_dates.extend(session_dates(store[symbol]["1h"]["yahoo"]))
        hourly_dates.extend(session_dates(store[symbol]["1h"]["alpaca"]))
    daily_window = set(sorted(set(daily_dates))[-DAILY_COMPARE_SESSIONS:])
    hourly_window = set(sorted(set(hourly_dates))[-HOURLY_COMPARE_SESSIONS:])

    alignment_rows = []
    diff_rows = []
    for symbol in SYMBOLS:
        for interval, window in (("1d", daily_window), ("1h", hourly_window)):
            yahoo = _in_dates(store[symbol][interval]["yahoo"], window)
            alpaca = _in_dates(store[symbol][interval]["alpaca"], window)
            aligned = align_frames(symbol, interval, yahoo, alpaca)
            alignment_rows.extend(aligned)
            for row in aligned:
                if row["presence"] != "BOTH_PRESENT":
                    continue
                diff = {
                    "symbol": symbol,
                    "interval": interval,
                    "timestamp": row["timestamp"],
                    "in_expected_session": row["in_expected_session"],
                }
                for field in OHLCV:
                    left = row[f"yahoo_{field}"]
                    right = row[f"alpaca_{field}"]
                    diff[f"yahoo_{field}"] = left
                    diff[f"alpaca_{field}"] = right
                    diff[f"abs_{field}"] = None if left is None or right is None else abs(right - left)
                    diff[f"pct_{field}"] = _pct(left, right)
                diff_rows.append(diff)

    pd.DataFrame(alignment_rows).to_csv(out / "alignment.csv", index=False)
    pd.DataFrame(diff_rows).to_csv(out / "ohlcv_diffs.csv", index=False)

    prepared = {}
    for symbol in SYMBOLS:
        prepared[symbol] = {}
        for name in ("yahoo", "alpaca"):
            prepared[symbol][name] = precompute_symbol_metrics(
                store[symbol]["1d"][name],
                store[symbol]["1h"][name],
            )

    scan_stamps = []
    for symbol in SYMBOLS:
        for name in ("yahoo", "alpaca"):
            frame = _in_dates(store[symbol]["1h"][name], hourly_window)
            if frame is None or frame.empty:
                continue
            for stamp in frame.index:
                if in_expected_session(stamp, "1h"):
                    scan_stamps.append(stamp)
    scan_stamps = list(pd.DatetimeIndex(scan_stamps).unique().sort_values()) if scan_stamps else []

    indicator_rows = []
    decision_rows = []
    changing_rows = []
    spearman_values = []
    pass_pairs = 0
    pass_same = 0
    top_pairs = 0
    top_same = 0
    candidate_pairs = 0
    candidate_changes = 0

    for stamp in scan_stamps:
        yahoo_rows = []
        alpaca_rows = []
        for symbol in SYMBOLS:
            yahoo_metrics = metrics_as_of(prepared[symbol]["yahoo"], stamp)
            alpaca_metrics = metrics_as_of(prepared[symbol]["alpaca"], stamp)
            for key, _label in INDICATORS:
                left = yahoo_metrics.get(key)
                right = alpaca_metrics.get(key)
                if left is None or right is None:
                    continue
                indicator_rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": _iso(stamp),
                        "indicator": key,
                        "yahoo": left,
                        "alpaca": right,
                        "abs_diff": abs(right - left),
                    }
                )
            yahoo_bar = _own_hourly_bar(store[symbol]["1h"]["yahoo"], stamp)
            alpaca_bar = _own_hourly_bar(store[symbol]["1h"]["alpaca"], stamp)
            yahoo_scored = evaluate_symbol(symbol, store[symbol]["1d"]["yahoo"], store[symbol]["1h"]["yahoo"], stamp, None, weights)
            alpaca_scored = evaluate_symbol(symbol, store[symbol]["1d"]["alpaca"], store[symbol]["1h"]["alpaca"], stamp, None, weights)
            yahoo_scored["sampled_hourly_bar"] = None if yahoo_bar is None else _iso(yahoo_bar)
            alpaca_scored["sampled_hourly_bar"] = None if alpaca_bar is None else _iso(alpaca_bar)
            yahoo_rows.append(yahoo_scored)
            alpaca_rows.append(alpaca_scored)

        yahoo_ranked = rank_snapshots(yahoo_rows)
        alpaca_ranked = rank_snapshots(alpaca_rows)
        yahoo_by = {row["symbol"]: row for row in yahoo_ranked}
        alpaca_by = {row["symbol"]: row for row in alpaca_ranked}
        yahoo_candidate = _candidate(yahoo_ranked)
        alpaca_candidate = _candidate(alpaca_ranked)
        candidate_pairs += 1
        candidate_same = yahoo_candidate == alpaca_candidate
        if not candidate_same:
            candidate_changes += 1
        yahoo_top = _top_set(yahoo_ranked)
        alpaca_top = _top_set(alpaca_ranked)
        top_pairs += 1
        if yahoo_top == alpaca_top:
            top_same += 1
        shared_ranks_y = []
        shared_ranks_a = []
        for symbol in SYMBOLS:
            y_row = yahoo_by[symbol]
            a_row = alpaca_by[symbol]
            y_pass = y_row["veto_status"] == "PASS"
            a_pass = a_row["veto_status"] == "PASS"
            pass_pairs += 1
            same_filter = y_pass == a_pass
            if same_filter:
                pass_same += 1
            y_score = y_row.get("final_score")
            a_score = a_row.get("final_score")
            score_delta = None if y_score is None or a_score is None else a_score - y_score
            y_rank = y_row.get("rank")
            a_rank = a_row.get("rank")
            rank_delta = None if y_rank is None or a_rank is None else a_rank - y_rank
            if y_rank is not None and a_rank is not None:
                shared_ranks_y.append(float(y_rank))
                shared_ranks_a.append(float(a_rank))
            kind = _classify_case(score_delta, same_filter, candidate_same, rank_delta)
            record = {
                "symbol": symbol,
                "timestamp": _iso(stamp),
                "class": kind,
                "yahoo_score": y_score,
                "alpaca_score": a_score,
                "score_diff": score_delta,
                "yahoo_rank": y_rank,
                "alpaca_rank": a_rank,
                "rank_diff": rank_delta,
                "yahoo_veto": y_row["veto_status"],
                "alpaca_veto": a_row["veto_status"],
                "yahoo_candidate": yahoo_candidate,
                "alpaca_candidate": alpaca_candidate,
                "yahoo_sampled_bar": y_row["sampled_hourly_bar"],
                "alpaca_sampled_bar": a_row["sampled_hourly_bar"],
            }
            decision_rows.append(record)
            if kind == "DECISION-CHANGING":
                changing_rows.append(
                    {
                        **record,
                        "affected": _affected_indicator(y_row, a_row),
                        "yahoo_reasons": ",".join(y_row.get("veto_reasons") or []),
                        "alpaca_reasons": ",".join(a_row.get("veto_reasons") or []),
                        **{f"yahoo_{key}": y_row.get(key) for key in SCORE_PARTS},
                        **{f"alpaca_{key}": a_row.get(key) for key in SCORE_PARTS},
                        **{f"yahoo_{key}": y_row.get(key) for key, _label in INDICATORS},
                        **{f"alpaca_{key}": a_row.get(key) for key, _label in INDICATORS},
                    }
                )
        rho = _spearman(shared_ranks_y, shared_ranks_a)
        if rho is not None:
            spearman_values.append(rho)

    pd.DataFrame(indicator_rows).to_csv(out / "indicators.csv", index=False)
    pd.DataFrame(decision_rows).to_csv(out / "decisions.csv", index=False)
    pd.DataFrame(changing_rows).to_csv(out / "decision_changing.csv", index=False)

    price_pct = [row[f"pct_{field}"] for row in diff_rows for field in PRICE_FIELDS if row[f"pct_{field}"] is not None]
    volume_pct = [row["pct_volume"] for row in diff_rows if row["pct_volume"] is not None]
    price_stats = _summary_stats(price_pct)
    volume_stats = _summary_stats(volume_pct)
    indicator_stats = {}
    for key, _label in INDICATORS:
        indicator_stats[key] = _summary_stats([row["abs_diff"] for row in indicator_rows if row["indicator"] == key])

    def _presence_share(rows, interval):
        chosen = [row for row in rows if row["interval"] == interval]
        n = len(chosen) or 1
        return {name: sum(1 for row in chosen if row["presence"] == name) / n for name in ("BOTH_PRESENT", "YAHOO_ONLY", "ALPACA_ONLY")}

    one_sided = [row for row in alignment_rows if row["presence"] != "BOTH_PRESENT"]
    in_session_one_sided = [row for row in one_sided if row["in_expected_session"]]
    out_session_one_sided = [row for row in one_sided if not row["in_expected_session"]]
    in_session_rows = [row for row in alignment_rows if row["in_expected_session"]]
    in_session_share = (len(in_session_one_sided) / len(in_session_rows)) if in_session_rows else 0.0

    volume_totals = []
    for symbol in SYMBOLS:
        for interval, window in (("1d", daily_window), ("1h", hourly_window)):
            volume_totals.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "yahoo_volume": float(_in_dates(store[symbol][interval]["yahoo"], window)["volume"].sum()) if len(_in_dates(store[symbol][interval]["yahoo"], window)) else 0.0,
                    "alpaca_volume": float(_in_dates(store[symbol][interval]["alpaca"], window)["volume"].sum()) if len(_in_dates(store[symbol][interval]["alpaca"], window)) else 0.0,
                }
            )

    non_empty = all(
        len(store[symbol][interval][name]) > 0
        for symbol in SYMBOLS
        for interval in ("1d", "1h")
        for name in ("yahoo", "alpaca")
    )
    data_quality_pass = non_empty and not errors
    price_pass = _pass_price(price_stats)
    volume_pass = _pass_volume(volume_stats)
    indicator_pass = all(_indicator_pass(key, stats, indicator_rows) for key, stats in indicator_stats.items())
    ranking_pass = bool(spearman_values) and float(pd.Series(spearman_values).median()) >= 0.99 and pass_pairs > 0 and pass_same == pass_pairs
    decision_pass = candidate_pairs > 0 and candidate_changes == 0 and pass_pairs > 0 and pass_same == pass_pairs

    summary = {
        "end": end_text,
        "daily_start": daily_start,
        "hourly_start": hourly_start,
        "daily_sessions": sorted(str(day) for day in daily_window),
        "hourly_sessions": sorted(str(day) for day in hourly_window),
        "errors": errors,
        "data_quality_pass": data_quality_pass,
        "price_pass": price_pass,
        "volume_pass": volume_pass,
        "indicator_pass": indicator_pass,
        "ranking_pass": ranking_pass,
        "decision_pass": decision_pass,
        "price_pct": price_stats,
        "volume_pct": volume_stats,
        "indicator_abs": indicator_stats,
        "presence_daily": _presence_share(alignment_rows, "1d"),
        "presence_hourly": _presence_share(alignment_rows, "1h"),
        "one_sided_in_session": len(in_session_one_sided),
        "one_sided_outside_session": len(out_session_one_sided),
        "in_session_one_sided_share": in_session_share,
        "spearman_median": None if not spearman_values else float(pd.Series(spearman_values).median()),
        "identical_pass_fail_pct": None if pass_pairs == 0 else 100.0 * pass_same / pass_pairs,
        "identical_top_n_pct": None if top_pairs == 0 else 100.0 * top_same / top_pairs,
        "candidate_changes": candidate_changes,
        "candidate_timestamps": candidate_pairs,
        "candidate_change_pct": None if candidate_pairs == 0 else 100.0 * candidate_changes / candidate_pairs,
        "volume_totals": volume_totals,
        "scan_timestamps": len(scan_stamps),
    }
    summary["conclusion"] = _verdict(summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "report.md").write_text(_report(summary, alignment_rows, changing_rows), encoding="utf-8")
    print(summary["conclusion"])
    print(f"report={out / 'report.md'}")
    return 0 if summary["conclusion"] != "ALPACA SIP REJECT" else 1


def _indicator_pass(key: str, stats: dict, rows: list[dict]) -> bool:
    """Median and 95th percentile absolute error within 5% and 15% of Yahoo's level."""
    if stats["n"] == 0 or stats["median"] is None or stats["p95"] is None:
        return False
    baselines = [abs(row["yahoo"]) for row in rows if row["indicator"] == key and row["yahoo"] not in (None, 0)]
    if not baselines:
        return False
    scale = float(pd.Series(baselines).median())
    if scale == 0:
        return False
    return stats["median"] / scale <= 0.05 and stats["p95"] / scale <= 0.15


def _fmt(value, digits=4):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _presence_line(share: dict) -> str:
    return (
        f"BOTH_PRESENT {_fmt(100 * share['BOTH_PRESENT'], 1)}% / "
        f"YAHOO_ONLY {_fmt(100 * share['YAHOO_ONLY'], 1)}% / "
        f"ALPACA_ONLY {_fmt(100 * share['ALPACA_ONLY'], 1)}%"
    )


def _examples(rows: list[dict]) -> list[dict]:
    inside = [row for row in rows if row["presence"] != "BOTH_PRESENT" and row["in_expected_session"]]
    outside = [row for row in rows if row["presence"] != "BOTH_PRESENT" and not row["in_expected_session"]]
    return (inside + outside)[:8]


def _report(summary: dict, alignment_rows: list[dict], changing_rows: list[dict]) -> str:
    price = summary["price_pct"]
    volume = summary["volume_pct"]
    lines = [
        "# Alpaca SIP vs Yahoo A/B",
        "",
        "Production provider is unchanged. Bars were not written to `market_bars.db`.",
        "Alpaca requests use `feed=sip` and `adjustment=all`. The exclusive end is at least 20 minutes before the run.",
        "No bars were forward-filled, interpolated, resampled, or invented.",
        "",
        f"- DATA QUALITY: {'PASS' if summary['data_quality_pass'] else 'FAIL'}",
        f"- PRICE CONSISTENCY: {'PASS' if summary['price_pass'] else 'FAIL'}",
        f"- VOLUME CONSISTENCY: {'PASS' if summary['volume_pass'] else 'FAIL'}",
        f"- INDICATOR CONSISTENCY: {'PASS' if summary['indicator_pass'] else 'FAIL'}",
        f"- RANKING CONSISTENCY: {'PASS' if summary['ranking_pass'] else 'FAIL'}",
        f"- TRADING-DECISION CONSISTENCY: {'PASS' if summary['decision_pass'] else 'FAIL'}",
        "",
        f"Conclusion: `{summary['conclusion']}`",
        "",
        "## Window",
        "",
        f"- Request end: `{summary['end']}`",
        f"- Daily fetch start: `{summary['daily_start']}` (EMA200 needs the long history; OHLCV compare uses the last {DAILY_COMPARE_SESSIONS} weekday sessions)",
        f"- Hourly fetch start: `{summary['hourly_start']}` (OHLCV and scans use the last {HOURLY_COMPARE_SESSIONS} weekday sessions)",
        f"- Daily sessions compared: {', '.join(summary['daily_sessions']) or 'none'}",
        f"- Hourly sessions compared: {', '.join(summary['hourly_sessions']) or 'none'}",
        f"- In-session hourly scan timestamps: {summary['scan_timestamps']}",
        "",
        "## Missing bars",
        "",
        f"- Daily presence: {_presence_line(summary['presence_daily'])}",
        f"- Hourly presence: {_presence_line(summary['presence_hourly'])}",
        f"- One-sided timestamps inside the expected session: {summary['one_sided_in_session']}",
        f"- One-sided timestamps outside the expected session: {summary['one_sided_outside_session']}",
        "",
        "Expected session: weekday daily labels, and hourly labels in [09:30, 16:00) America/New_York.",
        "Overnight, weekend, and 16:00 labels are outside. A one-sided bar is not called an error and is not called a close.",
        "",
        "### Examples",
        "",
    ]
    examples = _examples(alignment_rows)
    if not examples:
        lines.append("No one-sided timestamps.")
    for row in examples:
        lines.append(
            f"- {row['symbol']} {row['interval']} {row['timestamp']} {row['presence']} "
            f"in_session={row['in_expected_session']} "
            f"yahoo_neighbors=({row['yahoo_prev']}, {row['yahoo_next']}) "
            f"alpaca_neighbors=({row['alpaca_prev']}, {row['alpaca_next']})"
        )
    lines.extend(
        [
            "",
            "## OHLCV",
            "",
            "Percentage differences use Yahoo as the baseline, on BOTH_PRESENT bars only. Price fields are pooled separately from volume.",
            "",
            f"- Price MAPE {_fmt(price['mean'])}% / median {_fmt(price['median'])}% / p95 {_fmt(price['p95'])}% (n={price['n']})",
            f"- Volume MAPE {_fmt(volume['mean'])}% / median {_fmt(volume['median'])}% / p95 {_fmt(volume['p95'])}% (n={volume['n']})",
            "",
            "Total volume in each comparison window:",
            "",
        ]
    )
    for row in summary["volume_totals"]:
        lines.append(
            f"- {row['symbol']} {row['interval']}: Yahoo {row['yahoo_volume']:.0f} / Alpaca {row['alpaca_volume']:.0f}"
        )
    lines.extend(["", "## Indicators", "", "Absolute differences where both providers returned a finite production value.", ""])
    for key, label in INDICATORS:
        stats = summary["indicator_abs"][key]
        lines.append(
            f"- {label}: mean {_fmt(stats['mean'])} / median {_fmt(stats['median'])} / p95 {_fmt(stats['p95'])} (n={stats['n']})"
        )
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            f"- Spearman rank correlation, median across scan timestamps: {_fmt(summary['spearman_median'])}",
            f"- Identical pass/fail: {_fmt(summary['identical_pass_fail_pct'], 1)}%",
            f"- Identical Top-{TOP_N} membership: {_fmt(summary['identical_top_n_pct'], 1)}% "
            f"(with {len(SYMBOLS)} symbols this set is everyone who received a score)",
            f"- Timestamps where the PASS candidate changed: {summary['candidate_changes']} / {summary['candidate_timestamps']} "
            f"({_fmt(summary['candidate_change_pct'], 1)}%)",
            "",
            "The candidate is the best-ranked PASS symbol from `rank_snapshots`. A blocked rank-1 name is not the candidate.",
            "Each provider is scored from its own bars. A missing bar is never replaced with the other provider's bar.",
            "",
            "### Decision-changing cases",
            "",
        ]
    )
    if not changing_rows:
        lines.append("None.")
    for row in changing_rows:
        lines.append(
            f"- {row['symbol']} {row['timestamp']}: veto {row['yahoo_veto']} -> {row['alpaca_veto']} "
            f"({row['yahoo_reasons']} | {row['alpaca_reasons']}); "
            f"score {row['yahoo_score']} -> {row['alpaca_score']}; "
            f"rank {row['yahoo_rank']} -> {row['alpaca_rank']}; "
            f"candidate {row['yahoo_candidate']} -> {row['alpaca_candidate']}; "
            f"affected {row['affected']}; "
            f"sampled bars {row['yahoo_sampled_bar']} / {row['alpaca_sampled_bar']}"
        )
    if summary["errors"]:
        lines.extend(["", "## Fetch errors", ""])
        for error in summary["errors"]:
            lines.append(f"- {error['symbol']} {error['interval']}: {error['error']}")
    lines.extend(
        [
            "",
            "## Gates",
            "",
            "- Price PASS: median absolute percent <= 0.25 and p95 <= 1 on OHLC.",
            "- Volume PASS: median absolute percent <= 5 and p95 <= 15.",
            "- Indicator PASS: median and p95 absolute error within 5% and 15% of the Yahoo level, for every indicator.",
            "- Ranking PASS: median Spearman >= 0.99 and identical pass/fail.",
            "- Decision PASS: the PASS candidate never changes and pass/fail never changes.",
            "- ACCEPT requires every gate. A fetch that does not produce a comparable sample is NEEDS MORE TESTING. Any other miss is NEEDS MORE TESTING.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(run())

import numpy as np
import pandas as pd
import pytest

from src.data.provider import prepare_ohlcv
from src.features.indicators import atr, avg_dollar_volume, ema, macd_hist, rsi, volume_ratio
from src.universe.metrics import (
    HOURLY_STALE_AFTER,
    completed_daily,
    compute_symbol_metrics,
    metrics_as_of,
    precompute_symbol_metrics,
)


def _cutler_rsi(closes, window=14):
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    last = deltas[-window:]
    avg_gain = sum(max(delta, 0.0) for delta in last) / window
    avg_loss = sum(max(-delta, 0.0) for delta in last) / window
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def test_ema200_matches_recursive_definition_and_is_not_sma():
    close = pd.Series([float(i) for i in range(1, 251)])
    result = ema(close, 200)
    alpha = 2 / 201
    expected = float(close.iloc[0])
    for price in close.iloc[1:]:
        expected = alpha * float(price) + (1 - alpha) * expected
    assert result.iloc[-1] == pytest.approx(expected)
    assert result.iloc[-1] != pytest.approx(close.rolling(200).mean().iloc[-1])


def test_rsi14_matches_cutler_sma():
    closes = [44 + (i % 5) - (i % 3) * 0.5 for i in range(40)]
    df = pd.DataFrame({"close": closes})
    assert rsi(df, 14).iloc[-1] == pytest.approx(_cutler_rsi(closes, 14))
    assert rsi(df).iloc[-1] == pytest.approx(rsi(df, 14).iloc[-1])


def test_atr14_matches_wilder_rma():
    high = [10, 12, 11, 13, 12, 14, 15, 14, 16, 15, 17, 16, 18, 17, 19, 18.0]
    low = [price - 1.5 for price in high]
    close = [price - 0.5 for price in high]
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1})
    true_ranges = [high[0] - low[0]]
    for i in range(1, len(high)):
        true_ranges.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )
    alpha = 1 / 14
    expected = true_ranges[0]
    for true_range in true_ranges[1:]:
        expected = alpha * true_range + (1 - alpha) * expected
    assert atr(df).iloc[-1] == pytest.approx(expected)
    assert atr(df, 14).iloc[-1] == pytest.approx(expected)


def test_volume_ratio_is_current_over_rolling_mean_including_current_bar():
    volume = [10.0] * 19 + [20.0]
    df = pd.DataFrame({"close": np.ones(20), "volume": volume})
    assert volume_ratio(df, 20).iloc[-1] == pytest.approx(20 / 10.5)


def test_volume_ratio_is_zero_when_the_window_has_no_volume():
    df = pd.DataFrame({"close": np.ones(20), "volume": np.zeros(20)})
    assert volume_ratio(df, 20).iloc[-1] == pytest.approx(0.0)


def test_adv_is_rolling_mean_of_close_times_volume():
    close = np.arange(1, 26, dtype=float)
    volume = np.full(25, 2.0)
    df = pd.DataFrame({"close": close, "volume": volume})
    expected = float(np.mean(close[-20:] * volume[-20:]))
    assert avg_dollar_volume(df, 20).iloc[-1] == pytest.approx(expected)


def test_completed_daily_excludes_the_in_progress_new_york_session():
    prior = pd.bdate_range("2024-06-03", periods=3, tz="America/New_York")
    scan_day = prior[-1]
    frame = pd.DataFrame(
        {
            "open": [10, 11, 1],
            "high": [10, 11, 1],
            "low": [10, 11, 1],
            "close": [10, 11, 1],
            "volume": [100, 100, 100],
        },
        index=prior,
    )
    as_of = scan_day + pd.Timedelta(hours=15)
    kept = completed_daily(frame, as_of)
    assert list(kept["close"]) == [10, 11]


def test_metrics_ignore_same_day_daily_bar_and_future_hourly_bar():
    history = pd.bdate_range("2023-01-02", periods=210, tz="America/New_York")
    scan_day = history[-1]
    prior = history[:-1]
    daily_idx = prior.append(pd.DatetimeIndex([scan_day]))
    daily_close = [100.0] * len(prior) + [1.0]
    daily_volume = [1_000_000.0] * len(prior) + [50_000_000.0]
    daily = pd.DataFrame(
        {
            "open": daily_close,
            "high": daily_close,
            "low": daily_close,
            "close": daily_close,
            "volume": daily_volume,
        },
        index=daily_idx,
    )
    as_of = (scan_day + pd.Timedelta(hours=15)).tz_convert("UTC").floor("min")
    hourly_idx = pd.date_range(as_of - pd.Timedelta(hours=39), periods=40, freq="h", tz="UTC")
    future = as_of + pd.Timedelta(hours=1)
    hourly_idx = hourly_idx.append(pd.DatetimeIndex([future]))
    hourly_close = [100.0] * 40 + [999.0]
    hourly_volume = [1_000.0] * 40 + [1_000_000_000_000.0]
    hourly = pd.DataFrame(
        {
            "open": hourly_close,
            "high": [price + 0.1 for price in hourly_close],
            "low": [price - 0.1 for price in hourly_close],
            "close": hourly_close,
            "volume": hourly_volume,
        },
        index=hourly_idx,
    )

    metrics = compute_symbol_metrics(daily, hourly, as_of)
    causal = prepare_ohlcv(hourly).loc[:as_of]

    assert metrics["daily_close"] == pytest.approx(100.0)
    assert metrics["daily_ema200"] == pytest.approx(100.0)
    assert metrics["adv20"] == pytest.approx(100.0 * 1_000_000.0)
    assert metrics["hourly_close"] == pytest.approx(100.0)
    assert metrics["volume"] == pytest.approx(1_000.0)
    assert metrics["volume_ratio"] == pytest.approx(1.0)
    assert metrics["macd_hist"] == pytest.approx(float(macd_hist(causal).iloc[-1]))
    assert metrics["rsi14"] == pytest.approx(float(rsi(causal, 14).iloc[-1]))
    assert metrics["atr14"] == pytest.approx(float(atr(causal, 14).iloc[-1]))


def test_stale_hourly_bars_are_not_reported_as_current():
    daily_idx = pd.bdate_range("2023-01-02", periods=30, tz="America/New_York")
    daily = pd.DataFrame(
        {
            "open": 50.0,
            "high": 50.0,
            "low": 50.0,
            "close": 50.0,
            "volume": 100_000.0,
        },
        index=daily_idx,
    )
    as_of = (daily_idx[-1] + pd.Timedelta(days=1, hours=15)).tz_convert("UTC")
    hourly_end = as_of - pd.Timedelta(hours=5)
    hourly_idx = pd.date_range(hourly_end - pd.Timedelta(hours=30), periods=31, freq="h", tz="UTC")
    hourly = pd.DataFrame(
        {
            "open": 50.0,
            "high": 50.2,
            "low": 49.8,
            "close": 50.0,
            "volume": 10_000.0,
        },
        index=hourly_idx,
    )
    metrics = compute_symbol_metrics(daily, hourly, as_of)
    assert metrics["rsi14"] is None
    assert metrics["hourly_close"] is None
    assert metrics["daily_close"] == pytest.approx(50.0)
    assert metrics["adv20"] == pytest.approx(50.0 * 100_000.0)


def _prefix_metrics(daily, hourly, as_of):
    """Causal prefix definition, independent of the precomputed series."""
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts.tzinfo is None:
        as_of_ts = as_of_ts.tz_localize("UTC")
    else:
        as_of_ts = as_of_ts.tz_convert("UTC")
    as_of_ts = as_of_ts.floor("min")
    hourly_frame = prepare_ohlcv(hourly)
    daily_frame = prepare_ohlcv(daily)
    expected = {
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
    }
    hourly_hist = hourly_frame.loc[:as_of_ts] if not hourly_frame.empty else hourly_frame
    if not hourly_hist.empty and as_of_ts - hourly_hist.index[-1] <= HOURLY_STALE_AFTER:
        close = float(hourly_hist["close"].iloc[-1])
        expected["hourly_close"] = None if close <= 0 else close
        expected["volume"] = float(hourly_hist["volume"].iloc[-1])
        expected["rsi14"] = float(rsi(hourly_hist, 14).iloc[-1])
        expected["macd_hist"] = float(macd_hist(hourly_hist).iloc[-1])
        atr14 = float(atr(hourly_hist, 14).iloc[-1])
        expected["atr14"] = atr14
        if expected["hourly_close"] not in (None, 0):
            expected["atr_percent"] = atr14 / expected["hourly_close"]
        expected["volume_ratio"] = float(volume_ratio(hourly_hist, 20).iloc[-1])
    daily_hist = completed_daily(daily_frame, as_of_ts)
    if not daily_hist.empty:
        expected["daily_close"] = float(daily_hist["close"].iloc[-1])
        if len(daily_hist) >= 200:
            expected["daily_ema200"] = float(ema(daily_hist["close"], 200).iloc[-1])
        if len(daily_hist) >= 20:
            expected["adv20"] = float(avg_dollar_volume(daily_hist, 20).iloc[-1])
    return expected


def test_reused_indicator_series_matches_the_causal_prefix():
    daily_idx = pd.bdate_range("2023-01-02", periods=210, tz="America/New_York")
    daily_close = [100.0 + (i % 7) * 0.3 for i in range(210)]
    daily = pd.DataFrame(
        {
            "open": daily_close,
            "high": [price + 0.4 for price in daily_close],
            "low": [price - 0.4 for price in daily_close],
            "close": daily_close,
            "volume": [1_000_000.0 + (i % 4) * 10_000.0 for i in range(210)],
        },
        index=daily_idx,
    )
    as_of = (daily_idx[-1] + pd.Timedelta(hours=16)).tz_convert("UTC").floor("min")
    hourly_idx = pd.date_range(as_of - pd.Timedelta(hours=79), periods=80, freq="h", tz="UTC")
    hourly_close = [120.0 + ((i % 5) - 2) * 0.25 for i in range(80)]
    hourly = pd.DataFrame(
        {
            "open": hourly_close,
            "high": [price + 0.2 for price in hourly_close],
            "low": [price - 0.2 for price in hourly_close],
            "close": hourly_close,
            "volume": [10_000.0 + (i % 9) * 100.0 for i in range(80)],
        },
        index=hourly_idx,
    )
    gap_stamp = hourly_idx[24]
    hourly = hourly.drop(hourly.index[20:28])
    zero_at = hourly.index[10]
    hourly.loc[zero_at, "close"] = 0.0

    prepared = precompute_symbol_metrics(prepare_ohlcv(daily), prepare_ohlcv(hourly))
    stamps = list(hourly.index[-30:])
    stamps.append(hourly.index[-1] + pd.Timedelta(minutes=30))
    stamps.append(hourly.index[-1] + HOURLY_STALE_AFTER)
    stamps.append(hourly.index[-1] + HOURLY_STALE_AFTER + pd.Timedelta(minutes=1))
    stamps.append(gap_stamp)
    for stamp in stamps:
        sampled = metrics_as_of(prepared, stamp)
        direct = compute_symbol_metrics(daily, hourly, stamp)
        expected = _prefix_metrics(daily, hourly, stamp)
        for key, value in expected.items():
            assert sampled[key] == value, (stamp, key, sampled[key], value)
            assert direct[key] == value, (stamp, key, direct[key], value)
    stale = metrics_as_of(prepared, hourly.index[-1] + HOURLY_STALE_AFTER + pd.Timedelta(minutes=1))
    assert stale["rsi14"] is None
    assert stale["hourly_close"] is None
    assert stale["daily_close"] is not None
    gapped = metrics_as_of(prepared, gap_stamp)
    assert gapped["rsi14"] is None
    assert gapped["daily_ema200"] is not None
    zeroed = metrics_as_of(prepared, zero_at)
    assert zeroed["hourly_close"] is None
    assert zeroed["atr_percent"] is None
    assert zeroed["atr14"] is not None

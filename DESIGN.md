# Trade_stocks — Design

Universe v1 is the current milestone. It is an analytical scan of a predefined stock universe. It records metrics, hard vetoes, a normalized score, and a rank, then keeps every hourly snapshot. It does not place orders.

```text
DATA
  ↓
FEATURES
  ↓
HARD VETO
  ↓
SCORING
  ↓
RANKING
  ↓
PERSISTENCE
  ↓
UI
```

Hard veto is the first decision gate. A failing rule keeps the symbol visible and blocks scoring. Ranking orders the symbols that passed. A rank is not a trade.

## Universe

The universe is still the existing S&P 500 list in `src/data/scan.py`:

- `fetch_sp500_symbols()` reads the Wikipedia constituents table, with a small hardcoded fallback.
- `top_n_by_marketcap()` orders that list with `yfinance` `marketCap`.
- `build_universe_top_sp500(n)` returns the top `n` (default 100).

`scripts/run_universe.py --symbols AAPL,MSFT` evaluates an explicit list through the same metric, veto, score, and snapshot path. That flag is a local convenience. It does not change how the S&P universe is built. `scripts/scheduler.py` uses `UNIVERSE_SYMBOLS` when set, and otherwise `build_universe_top_sp500`.

## Market data

Two timeframes, both OHLCV (`timestamp`, `open`, `high`, `low`, `close`, `volume`):

| Timeframe | Role |
| --- | --- |
| Daily | Long-term trend (`EMA200`) and liquidity (`ADV20`) |
| Hourly | RSI(14), MACD histogram, ATR(14), ATR%, VolumeRatio, and the price shown in the ranking |

`src/data/provider.py` defines `MarketDataProvider.get_bars(symbol, start, end, interval)`. `YFinanceMarketDataProvider` is the default and calls the existing `fetch_historical_yfinance` helper. Metric, veto, scoring, and ranking code only see normalized frames from `prepare_ohlcv` (lowercase columns, UTC index, floored to the minute). A future Alpaca provider can implement `get_bars` and replace the yfinance class at `run_universe` without rewriting those layers. `AlpacaIngest` remains the existing broker/bars wrapper and is not the universe default.

Daily trend rule: a daily bar counts only when its America/New_York timestamp is strictly before the scan's New York session midnight. The in-progress session cannot enter `EMA200`. Hourly indicators use bars at or before the scan time. If the exact hour is missing, the previous bar may be used when it is at most two hours old. Older hourly bars are left blank.

The default fetch windows are about 520 calendar days of daily bars (so `EMA200` has 200 completed sessions) and 60 calendar days of hourly bars.

## Metrics

Implemented in `src/features/indicators.py` and assembled by `src/universe/metrics.py`.

### Daily EMA(200)

`ema(close, 200)` is a recursive EMA with `adjust=False` (`alpha = 2 / 201`), seeded on the first close. It is computed from the daily close series in this process. The trend comparison is `latest completed daily close > latest daily EMA200`. Fewer than 200 completed daily bars leaves `daily_ema200` empty.

### Hourly RSI(14)

Cutler's RSI, which is the implementation already in the project: SMA of gains and SMA of losses over 14 hourly changes. This is not Wilder's recursive RSI. A zero average loss uses a tiny epsilon so a run of gains approaches 100.

Safety band used by the veto: `35 <= RSI <= 75`.

### Hourly MACD histogram

Existing `macd_hist`: EMA(12) − EMA(26), minus the EMA(9) signal, all with `adjust=False`. No other MACD variant is used.

### Hourly ATR(14) and ATR%

True range is the max of `high-low`, `|high − previous close|`, and `|low − previous close|`. The first bar uses `high − low`. Smoothing is Wilder's RMA via `ewm(alpha=1/14, adjust=False)`, seeded on the first true range (no SMA seed).

`atr_percent = ATR14 / current hourly close`.

### VolumeRatio

`current hourly volume / rolling_mean(hourly volume, 20)`. The window includes the current bar. A window whose mean volume is zero yields `0`. This is a participation ratio. The legacy `volume_spike` helper is unchanged and is not used by Universe v1.

### ADV20

`rolling_mean(close * volume, 20)` on the **daily** series. The $5,000,000 threshold is a daily liquidity floor, so ADV20 stays on daily bars while VolumeRatio stays on hourly bars. Fewer than 20 completed daily bars leaves `adv20` empty.

## Hard veto

`src/universe/veto.py`. Every rule is evaluated on its own. `veto_reasons` lists every failure. `veto_status` is the earliest failure in this order:

1. `BLOCKED_TREND` — completed `daily_close <= daily_EMA200`
2. `BLOCKED_RSI` — hourly RSI `< 35` or `> 75`
3. `BLOCKED_VOLATILITY` — `ATR14 / hourly_close > 0.04` (`0.04` passes)
4. `BLOCKED_VOLUME` — `VolumeRatio < 0.8` (`0.8` passes)
5. `BLOCKED_LIQUIDITY` — daily `ADV20 < 5,000,000` (`5,000,000` passes)
6. `BLOCKED_EARNINGS` — earnings inside the next 48 hours, only when a reliable calendar says so
7. `BLOCKED_INSUFFICIENT_DATA` — a required input is missing or non-finite

`PASS` means the reasons list is empty. Missing numbers are not compared, so a NaN cannot pass a rule by accident. `BLOCKED_INSUFFICIENT_DATA` is an extra status beyond the named market rules: the scan still has to record symbols whose history cannot support a metric.

### Earnings

`src/universe/earnings.py` is the provider boundary. `UnavailableEarningsProvider` is the default. `available` is false and `within_hours` returns `None`. The snapshot field `earnings_check` is then `unavailable`. `BLOCKED_EARNINGS` is not applied. The UI states that the earnings veto is pending.

An available provider that returns `True` sets `earnings_check` to `within_48h` and adds `BLOCKED_EARNINGS`. A return of `False` sets `earnings_check` to `clear`. A missing date from an otherwise available provider stays `None` and does not block that symbol. No zero-filled earnings series is treated as a calendar.

The legacy feature bundle in `compute_all_features` now stores NaN for `earnings_proximity`, `news_sentiment`, and `short_interest`. Universe v1 does not read those series.

## Scoring

Only `veto_status = PASS` is scored. `src/universe/scoring.py` maps each factor onto `[0, 10]` and combines them with weights from `config/universe_weights.yaml`:

| Factor | Weight | Inputs |
| --- | --- | --- |
| Trend | 30% | Distance of daily close above EMA200 |
| Momentum | 30% | Hourly RSI and MACD histogram |
| Volatility risk | 20% | Hourly ATR% |
| Volume / liquidity | 20% | VolumeRatio and daily ADV20 |

Weights must sum to 1, so the final score stays on `[0, 10]`.

Normalization (scale anchors, not extra vetoes):

- Trend: `0` at the EMA, `10` at 10% above the EMA, clamped.
- RSI: `0` at 35, `10` at 75, clamped.
- MACD: histogram divided by hourly close; `0` at −2% of price, `10` at +2% of price, clamped.
- Momentum is the equal blend of the RSI and MACD components.
- Volatility risk: `10` at ATR% = 0, `0` at ATR% = 0.04, clamped. Lower ATR% scores higher.
- VolumeRatio: `0` at 0.8, `10` at 2.0, clamped.
- ADV20: `0` at $5,000,000, `10` at 20× that level, on a log10 scale, clamped.
- Volume/liquidity is the equal blend of those two components.

`apply_score` clears `trend_score`, `momentum_score`, `volatility_score`, `volume_liquidity_score`, and `final_score` unless the status is `PASS`. `rank_snapshots` clears them again for any non-PASS row, including a row that arrived with a leftover score.

`src/scoring/engine.py` is the older raw weighted sum of feature values. `TradingBotManager` still calls it. The universe pipeline does not.

## Ranking

`rank_snapshots` assigns rank `1..N` to PASS rows, highest `final_score` first. Equal scores break ties by symbol. Blocked rows keep their veto status and reasons, with empty rank and empty scores, and are listed after every passing row. They do not compete for a candidate rank.

The pipeline does not select a position and does not call the broker.

## Snapshot schema

SQLite database, default `artifacts/universe.db`, table `universe_snapshots`. Each scan inserts rows. `UNIQUE(scan_timestamp, symbol)` plus `INSERT OR IGNORE` leaves an existing snapshot unchanged. History is the point of the table: a later hour is a new row.

| Column | Meaning |
| --- | --- |
| `scan_timestamp` | Evaluation time, UTC ISO-8601 |
| `symbol` | Ticker |
| `hourly_close` | Hourly close used as price |
| `daily_close` | Last completed daily close |
| `daily_ema200` | EMA(200) of that daily history |
| `rsi14` | Hourly RSI(14) |
| `macd_hist` | Hourly MACD histogram |
| `atr14` | Hourly ATR(14) |
| `atr_percent` | ATR14 / hourly close |
| `volume` | Current hourly volume |
| `volume_ratio` | Hourly VolumeRatio |
| `adv20` | 20-day average dollar volume |
| `veto_status` | `PASS` or the primary block |
| `veto_reasons` | JSON list of every failed rule |
| `earnings_check` | `unavailable`, `clear`, or `within_48h` |
| `trend_score` | Trend factor, or null when blocked |
| `momentum_score` | Momentum factor, or null when blocked |
| `volatility_score` | Volatility-risk factor, or null when blocked |
| `volume_liquidity_score` | Volume/liquidity factor, or null when blocked |
| `final_score` | Weighted score on `[0, 10]`, or null when blocked |
| `rank` | Candidate rank, or null when blocked |

`earnings_check` is stored in addition to the minimum forensic columns so the earnings veto can be seen as inactive.

The latest scan is also exported as `artifacts/universe_<timestamp>.csv` and `artifacts/scores_<timestamp>.csv` (`symbol`, `price`, `score`, `veto_status`, `rank`). The existing FastAPI route `GET /scores` still reads the latest `scores_*.csv`.

A run snapshots the last 24 shared hourly bars by default (`--lookback`). Re-running the same bars does not duplicate them. The hourly scheduler (`scripts/scheduler.py`, cron minute 0) calls this same pipeline. Its previous behavior was a daily-bar raw score CSV; that job now persists Universe v1 snapshots. It still does not place orders.

## UI

`streamlit run src/ui/streamlit_app.py`

The page reads the snapshot database and shows the latest scan:

`Rank`, `Symbol`, `Price`, `Status`, `Score`, `RSI`, `MACD Histogram`, `ATR %`, `Volume Ratio`, `ADV`

`ATR %` is `atr_percent * 100` (percent points). Passing symbols are ranked from 1. Blocked symbols stay in the table with an empty rank and an empty score.

A symbol selector loads that symbol's most recent 24 snapshots, oldest first, and draws line charts for RSI, Score, ATR %, and Volume Ratio. Hours that failed a hard veto have an empty score, so the score line breaks there. The same 24 rows are listed with time, veto status, and every veto reason. The detail panel also shows the latest veto status, the latest reasons, and the earnings-check value. When the latest scan's earnings checks are all `unavailable`, the page says the earnings veto is pending.

The FastAPI app in `src/ui/app.py` is unchanged and remains a JSON view of the scores CSV.

## How to run

From the repository root, with the project virtualenv:

```text
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\run_universe.py --symbols AAPL,MSFT,NVDA,AMZN,META,JPM,XOM,JNJ
.venv\Scripts\streamlit run src/ui/streamlit_app.py
```

`--size 100` builds the top 100 S&P names. That path calls `yfinance` once per constituent for market cap before the price download, so an explicit symbol list is the practical local run. `UNIVERSE_DB` overrides the SQLite path. `UNIVERSE_SYMBOLS` overrides the scheduler's symbol list.

## Known limitations

- Earnings dates are not available from the current provider. The veto interface exists and the check is reported as `unavailable`.
- RSI is Cutler's SMA form, matching the existing helper, not Wilder's RSI.
- ATR uses Wilder's recursive average seeded on the first true range, not an SMA-seeded ATR.
- ADV20 is daily dollar volume. VolumeRatio is hourly.
- The two-hour hourly fallback and the New York session cutoff are alignment rules so a scan does not read the future. They are not trading rules.
- Score caps (10% above EMA200, ±2% MACD, VolumeRatio 2.0, 20× ADV) only keep the weighted score on a common 0–10 scale.
- yfinance `history()` adjusted prices are whatever that library returns. The universe does not add a second adjustment.
- Wikipedia symbols such as `BRK.B` are not remapped to the yfinance `BRK-B` form. That behavior is unchanged.
- One pipeline run backfills the last 24 hourly bars already on disk. After that, new hours append when the scan runs again. The chart's 24 points are the latest stored market hours, which can span more than 24 clock hours because the cash session is shorter than a calendar day, and a weekend has no new bars.
- The top-100 market-cap sort is slow and rate-limit prone. The evaluation path itself does not require the full sort when symbols are passed in.
- `src/logging_config.py` and the trades DDL in `src/persistence/store.py` had escaped quotes that made those modules fail to import. Both quote sequences are real Python strings now. The trades table shape is unchanged. `scripts/run_backtest.py`, `scripts/report_backtest.py`, and `scripts/report_equity.py` still contain the same class of escaped quotes and are outside this milestone.

## Pre-existing modules outside this milestone

These files are still in the repo. Universe v1 does not call them to trade.

- `src/scoring/engine.py` — legacy raw weighted sum (`config/weights.yaml`).
- `src/bot/manager.py` — single-position paper bot (limit entry, 2% stop, 4% target, 24-hour hold).
- `src/execution/alpaca_adapter.py`, `src/risk/manager.py` — paper orders and position/loss limits.
- `src/backtest/engine.py`, `src/backtest/execution.py` — single-name and portfolio backtests with a local fill simulator.
- `src/strategy/simple_mean_reversion.py` — existing sample strategy used by the backtest scripts.
- `src/ui/app.py` — FastAPI `/scores`.

## FUTURE / NOT IMPLEMENTED

- Order Execution Guard
- Position Guardian
- dynamic TP/SL
- multi-position portfolio selection
- adaptive thresholds
- market regime detection
- advanced volume analysis
- live trading

Also not in this milestone: a reliable earnings calendar, news sentiment, short interest, replacing yfinance with Alpaca market data for the hourly scan, and cross-sectional z-score scoring. The legacy raw scorer is not the Universe v1 score.

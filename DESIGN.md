# Trade_stocks — Design

Universe v1 is the current milestone. It is an analytical scan of a predefined stock universe. It records metrics, hard vetoes, a normalized score, and a rank, then keeps every hourly snapshot. It does not place orders.

```text
DATA (daily + hourly OHLCV)
  ↓
FEATURES (causal metrics)
  ↓
HARD VETO (status only)
  ↓
SCORING (PASS and BLOCKED, when inputs are finite)
  ↓
RANKING
  ↓
PERSISTENCE (universe_snapshots)
  ↓
UI

After the latest ranking only:
  up to 10 PASS candidates
    ↓
  1-minute enrichment (raw facts, separate table)
```

A hard veto records risk status. It does not remove a symbol from scoring. Every symbol with valid metric inputs receives a score. Ranking orders all scored symbols by final score. `PASS` and `BLOCKED` stay status information, not a ranking filter. A rank is not a trade. Minute enrichment does not feed back into the score, the rank, the veto, or an order.

## Universe

The evaluation pool is the S&P 500 constituent list in `src/data/scan.py`.

- `fetch_sp500_symbols()` reads the Wikipedia constituents table with an identified User-Agent. A failed or short response raises `UniverseResolutionError`. It does not substitute a sample list.
- A table with fewer than 400 symbols is rejected.
- Wikipedia share classes are normalized at that boundary: `BRK.B` becomes `BRK-B` and `BF.B` becomes `BF-B`. Those symbols stay in the universe.
- `build_sp500_universe(limit)` returns that list. `limit` truncates it in constituent-list order. It does not sort by market cap.
- `top_n_by_marketcap()` and `build_universe_top_sp500(n)` still exist. The Universe v1 scan and the hourly scheduler do not call them.

`scripts/run_universe.py --symbols AAPL,MSFT` evaluates an explicit list through the same metric, veto, score, and snapshot path. With no `--symbols`, the scan scores the full S&P 500 pool. `--size` or `UNIVERSE_SIZE`, when set, caps that pool in constituent order. `scripts/scheduler.py` uses `UNIVERSE_SYMBOLS` when set, and otherwise the same pool. The scheduler runs at minute 0 and does not place orders.

Top 100 is applied after scoring: `rank <= 100` by `final_score`. It is not a market-cap filter and not a PASS filter. The database keeps every evaluated symbol.

## Market data

Two timeframes feed the score. Both are OHLCV (`timestamp`, `open`, `high`, `low`, `close`, `volume`):

| Timeframe | Role |
| --- | --- |
| Daily | Long-term trend (`EMA200`) and liquidity (`ADV20`) |
| Hourly | RSI(14), MACD histogram, ATR(14), ATR%, VolumeRatio, and the price shown in the ranking |

`src/data/provider.py` defines `MarketDataProvider.get_bars(symbol, start, end, interval)`. `prepare_ohlcv` is the only frame the metric, veto, scoring, and ranking code sees: lowercase columns, UTC index, floored to the minute, de-duplicated, rows with a missing close dropped. Naive timestamps are interpreted as UTC.

`AlpacaMarketDataProvider` is the production default. `run_universe` constructs it when no provider is passed, and the scheduler uses that same path. It requests `https://data.alpaca.markets/v2/stocks/bars` with `feed=sip` and `adjustment=all`. A requested end inside the last 15 minutes is pulled back to that delay, because this SIP subscription rejects the whole query when the end reaches recent data. Intervals are `1d` → `1Day`, `1h` → `1Hour`, and `1m` → `1Min`. `BRK-B` and `BF-B` are sent as `BRK.B` and `BF.B`. The end bound is exclusive. Credentials are `ALPACA_API_KEY` and `ALPACA_API_SECRET`. `AlpacaIngest` remains the older broker wrapper and is not the universe default.

`YFinanceMarketDataProvider` is no longer used by the production Universe path. It still delegates to `fetch_historical_yfinance` for tests and for `scripts/ab_alpaca_yahoo.py`.

`scripts/ab_alpaca_yahoo.py` is an offline comparison of Yahoo and Alpaca SIP for AAPL, MSFT, BRK-B, NVDA, and POOL. It does not select the production provider and does not open `market_bars.db`.

A missing `ALPACA_API_KEY` or `ALPACA_API_SECRET`, or an Alpaca `401`/`403`, raises `MarketDataUnavailable` on the first request. `BarStore` does not count that as a per-symbol `failed` sync, and `run_universe` writes no snapshot. One symbol whose fetch raises for another reason stays in the scan as insufficient data when another symbol still has bars. When every sync is `failed` across two or more symbols, the same exception stops the scan before `save_universe_snapshots`. The `failed` total is the existing `BarStore.actions` counter.

Daily trend rule: a daily bar counts only when its America/New_York timestamp is strictly before the scan's New York session midnight. The in-progress session cannot enter `EMA200`. Hourly indicators use the last bar at or before the scan time, and only when that bar is at most two hours old (`HOURLY_STALE_AFTER`). Older hourly data is left blank. These are alignment rules so a scan does not read the future. They are not trading rules.

Default fetch windows are about 520 calendar days of daily bars (so `EMA200` has 200 completed sessions) and 60 calendar days of hourly bars. One run snapshots the last 24 shared hourly bars by default (`--lookback`).

### Bar cache

`src/data/bar_store.py` keeps raw bars in `artifacts/market_bars.db` (`MARKET_BARS_DB` overrides the path). Snapshots stay in the universe database. The two stores are not the same file.

`market_bars` is one row per `(symbol, interval, bar_timestamp)`:

| Column | Meaning |
| --- | --- |
| `symbol`, `interval`, `bar_timestamp` | Identity. Timestamp is UTC, floored to the minute |
| `open`, `high`, `low`, `close`, `volume` | Canonical OHLCV for that bar |
| `session_type` | New York session label for a valid hourly clock bar, otherwise NULL |
| `observed_minutes` | Distinct 1-minute bars inside the hour, or NULL when that lookup has not run |
| `expected_minutes` | 60 for a valid hourly clock bar |
| `coverage_ratio` | `observed_minutes / 60`, or NULL when `observed_minutes` is NULL |

An older database gains the four session columns on open. The OHLCV upsert does not clear them. A rebuild that rewrites bars restores the session values that were already stored. `load_window` returns OHLCV only, so scoring does not see the session columns.

`market_coverage` is a different fact. One row per `(symbol, interval)` records `window_start`, `checked_through`, and `latest_bar`: which fetch window was successfully checked. Freshness uses that row, not the minimum or maximum stored bar. A closed market can legitimately have no bar. `coverage_ratio` is not written here.

`update_hourly_session` updates session columns on an existing `1h` row. It does not insert a bar. A weekend, holiday, or other missing hour does not become a zero row. An invalid timestamp is not given a session type.

### Hourly session clock

`src/data/session_bars.py` names an hourly bar from the America/New_York wall clock of its timestamp. The UTC hour is never the classifier. The same ET label stays in the same session on EST and EDT, because the classification converts to `America/New_York` first. The daylight-saving fold and gap are at 02:00 ET, outside this session.

Canonical hourly labels are `04:00` through `19:00` ET. The window of a bar is `[H:00, H+1:00)`.

| ET hour label | `session_type` | What the hour contains |
| --- | --- | --- |
| 04:00–08:00 | `PRE_MARKET` | The whole clock hour is before 09:00 |
| 09:00 | `MIXED` | 09:00–09:29 pre-market and 09:30–09:59 regular, including the open, in one bar |
| 10:00–15:00 | `REGULAR` | The whole clock hour is inside the regular session. The 15:00 bar ends at 15:59 |
| 16:00 | `MIXED` | The 16:00 close boundary, then after-hours through 16:59, in one bar |
| 17:00–19:00 | `AFTER_HOURS` | The whole clock hour is after 17:00. The 19:00 bar ends at 20:00 exclusive |

A timestamp outside `[04:00, 20:00)` ET, or any timestamp that is not one of those exact hour labels, has no session type. `09:30` and `16:30` are off-grid, not `MIXED`. The two `MIXED` hours share a label and are distinguished by the timestamp. The schema does not split pre-market minutes from regular minutes, and it does not split the closing print from the rest of the 16:00 hour.

`expected_minutes` is 60, the clock slots `H:00`–`H:59`. `observed_minutes` counts distinct 1-minute trade bars inside the hour. It stays NULL until that lookup runs. NULL is not zero. A lookup that finds no minute bars stores 0. A missing minute means no trade print in that minute. It is not a closed market, a data error, a confidence weight, or a filter. `coverage_ratio` is that count divided by 60, and only when the count exists.

Alpaca SIP `1h` bars for this clock match the aggregation of the underlying 1-minute bars. The `1h` response fields are `t`, `o`, `h`, `l`, `c`, `v`, `n`, and `vw`. `n` is a trade count and `vw` is VWAP. Neither is a minute count, so `observed_minutes` is not derived from the hourly response.

## Metrics

Implemented in `src/features/indicators.py` and assembled by `src/universe/metrics.py`.

### Daily EMA(200)

`ema(close, 200)` is a recursive EMA with `adjust=False` (`alpha = 2 / 201`), seeded on the first close. The trend comparison is `latest completed daily close > latest daily EMA200`. Fewer than 200 completed daily bars leaves `daily_ema200` empty.

### Hourly RSI(14)

Cutler's RSI: SMA of gains and SMA of losses over 14 hourly changes. This is not Wilder's recursive RSI. A zero average loss uses a tiny epsilon so a run of gains approaches 100. The veto band is `35 <= RSI <= 75`.

### Hourly MACD histogram

`macd_hist`: EMA(12) − EMA(26), minus the EMA(9) signal, all with `adjust=False`.

### Hourly ATR(14) and ATR%

True range is the max of `high-low`, `|high − previous close|`, and `|low − previous close|`. The first bar uses `high − low`. Smoothing is Wilder's RMA via `ewm(alpha=1/14, adjust=False)`, seeded on the first true range.

`atr_percent = ATR14 / current hourly close`.

### VolumeRatio

`current hourly volume / rolling_mean(hourly volume, 20)`. The window includes the current bar. A window whose mean volume is zero yields `0`. This is a participation ratio. The legacy `volume_spike` helper is unchanged and is not used by Universe v1.

### ADV20

`rolling_mean(close * volume, 20)` on the daily series. The $5,000,000 threshold is a daily liquidity floor, so ADV20 stays on daily bars while VolumeRatio stays on hourly bars. Fewer than 20 completed daily bars leaves `adv20` empty.

## Hard veto

`src/universe/veto.py`. Every rule is evaluated on its own. `veto_reasons` lists every failure. `veto_status` is the earliest failure in this order:

1. `BLOCKED_TREND` — completed `daily_close <= daily_EMA200`
2. `BLOCKED_RSI` — hourly RSI `< 35` or `> 75`
3. `BLOCKED_VOLATILITY` — `ATR14 / hourly_close > 0.04` (`0.04` passes)
4. `BLOCKED_VOLUME` — `VolumeRatio < 0.8` (`0.8` passes)
5. `BLOCKED_LIQUIDITY` — daily `ADV20 < 5,000,000` (`5,000,000` passes)
6. `BLOCKED_EARNINGS` — earnings inside the next 48 hours, only when a reliable calendar says so
7. `BLOCKED_INSUFFICIENT_DATA` — a required input is missing, non-finite, or negative where a ratio cannot be negative

`PASS` means the reasons list is empty. Missing numbers are not compared, so a NaN cannot pass a rule by accident. A veto does not clear a score.

### Earnings

`src/universe/earnings.py` is the provider boundary. `UnavailableEarningsProvider` is the default. `available` is false and `within_hours` returns `None`. The snapshot field `earnings_check` is then `unavailable`. `BLOCKED_EARNINGS` is not applied. The UI states that the earnings veto is pending.

An available provider that returns `True` sets `earnings_check` to `within_48h` and adds `BLOCKED_EARNINGS`. A return of `False` sets `earnings_check` to `clear`. A missing date from an otherwise available provider stays `None` and does not block that symbol. No zero-filled earnings series is treated as a calendar.

The legacy feature bundle in `compute_all_features` stores NaN for `earnings_proximity`, `news_sentiment`, and `short_interest`. Universe v1 does not read those series.

## Scoring

`src/universe/scoring.py` maps each factor onto `[0, 10]` and combines them with weights from `config/universe_weights.yaml`. PASS and BLOCKED rows use the same formulas when the required inputs are finite:

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

`apply_score` computes those fields whenever `daily_close`, `daily_ema200`, `hourly_close`, `rsi14`, `macd_hist`, `atr_percent`, `volume_ratio`, and `adv20` are finite and usable (denominators are not zero, and ATR%, VolumeRatio, and ADV20 are not negative). Missing or invalid inputs clear the five score fields. No substitute score is invented.

`src/scoring/engine.py` is the older raw weighted sum of feature values (`config/weights.yaml`). `TradingBotManager` still calls it. The universe pipeline does not.

## Ranking

`rank_snapshots` assigns rank `1..N` to every row with a final score, highest `final_score` first. Equal scores break ties by symbol. PASS and BLOCKED rows share that ordering. Rows without a score keep an empty rank and are listed after the ranked rows. Top 100 is `rank <= 100` (`TOP_N`). The pipeline persists every evaluated symbol, not only the top 100.

### Who may receive 1-minute bars

`src/universe/minute_candidates.py` runs only after `rank_snapshots`. Eligible rows are `PASS` rows that already have a rank. They are taken best rank first, up to `max_candidates_for_1m` in `config/universe.yaml`. The code constant is `MAX_CANDIDATES_FOR_1M` and the configured value is 10. The number is not hardcoded in the pipeline. Fewer than 10 eligible rows use all of them. Zero eligible rows means no 1-minute request. BLOCKED rows and rows without a rank are not eligible, even when their score is higher.

The pipeline does not select a position and does not call the broker.

## 1-minute enrichment

`src/data/minute_enrichment.py` runs after the latest ranking, and only for the candidates just selected. It does not run for the rest of the universe and it does not run before ranking. It uses the same provider object the scan used. A non-empty minute window is an Alpaca SIP `1Min` request.

`scan_timestamp` and `hour_timestamp` are different facts. The latest snapshot's `scan_timestamp` is the scan clock in UTC, floored to the minute. Earlier lookback snapshots stay on the hourly bar timestamps. `executed_at` stays the time the row was written. `hour_timestamp` is the last hourly bar at or before that scan, and only when it is within `HOURLY_STALE_AFTER` (2 hours). A staler bar is not used. With no valid hourly bar the symbol keeps the existing insufficient-data result.

The causal minute window is `[hour_timestamp, min(hour_timestamp + 1h, scan_timestamp))`. A minute at or after the scan is out. A minute of the next clock hour is out, even when the scan is already inside that next hour. A scan at 17:02 that still uses the 17:00 bar keeps 17:00 and 17:01. A scan at 17:02 that still uses the 16:00 bar keeps 16:00 through 16:59. `observed_minutes`, `coverage_ratio`, and `expected_minutes` stay on that hourly label. `expected_minutes` remains 60. The 16:00 bar does not gain minutes from 17:00 onward.

The latest score uses the same scan clock and the same freshness rule. Score formulas are unchanged. A 5-minute or 10-minute return requires the exact minute that many minutes earlier. A missing minute is not filled.

The result is raw data. It is stored in `minute_enrichment` inside the universe database, keyed by `(scan_timestamp, symbol)`. It is not written into `universe_snapshots` and it does not change a score, a rank, a veto, or an order. When the hour is a valid session label, the same lookup also updates `observed_minutes` and `coverage_ratio` on the existing `market_bars` row.

| Field | Meaning |
| --- | --- |
| `scan_timestamp`, `symbol`, `hour_timestamp` | Scan identity and the hourly bar |
| `session_type`, `expected_minutes` | Session label and 60, when the hour is on the clock grid |
| `observed_minutes`, `coverage_ratio` | Minute count and `observed_minutes / 60`. NULL if the lookup did not run. `0` / `0.0` if it ran and found nothing |
| `first_price`, `last_price` | Open of the first minute and close of the last minute |
| `intrahour_return` | `(last_price - first_price) / first_price` |
| `intrahour_high`, `intrahour_low` | Max high and min low of those minutes |
| `close_position` | `(last_price - intrahour_low) / (intrahour_high - intrahour_low)` |
| `hourly_signal` | `UP`, `DOWN`, or `FLAT` from the hourly bar's own open and close |
| `minutes_with_signal`, `minutes_against_signal` | Minutes whose open-to-close move agrees or disagrees with that signal. A flat minute is in neither count |
| `minute_volume` | Sum of 1-minute volume |
| `top5_volume_share` | Volume of the five heaviest minutes divided by `minute_volume` |
| `last10_volume_share` | Volume of clock minutes `H:50`–`H:59` divided by `minute_volume` |
| `return_5m`, `return_10m` | Last close versus the close exactly 5 or 10 minutes earlier, when that minute exists |
| `mfe`, `mae` | Largest favorable and adverse move from `first_price` to the intrahour high/low, in the direction of `hourly_signal` |

## Snapshot schema

SQLite database, default `artifacts/universe.db` (`UNIVERSE_DB` overrides it), table `universe_snapshots`. Each scan inserts rows. `UNIQUE(scan_timestamp, symbol)` plus `INSERT OR IGNORE` leaves an existing snapshot unchanged. History is the point of the table: a later decision is a new row. The latest `scan_timestamp` is the scan clock, UTC, floored to the minute. Earlier rows from the same run keep their hourly bar timestamps. `executed_at` is the wall clock of the write and is never copied from `scan_timestamp`. An older database gains `executed_at` on open.

| Column | Meaning |
| --- | --- |
| `scan_timestamp` | Decision time, UTC ISO-8601. The latest row is the scan clock. Earlier lookback rows are hourly bar timestamps |
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
| `trend_score` | Trend factor, or null when scoring inputs are missing or invalid |
| `momentum_score` | Momentum factor, or null when scoring inputs are missing or invalid |
| `volatility_score` | Volatility-risk factor, or null when scoring inputs are missing or invalid |
| `volume_liquidity_score` | Volume/liquidity factor, or null when scoring inputs are missing or invalid |
| `final_score` | Weighted score on `[0, 10]`, or null when scoring inputs are missing or invalid |
| `rank` | Score rank, or null when there is no final score |
| `executed_at` | When this row was written |

The latest scan is also exported as `artifacts/universe_<timestamp>.csv` and `artifacts/scores_<timestamp>.csv` (`symbol`, `price`, `score`, `veto_status`, `rank`). The FastAPI route `GET /scores` still reads the latest `scores_*.csv`.

`artifacts/` is generated output. It is gitignored. The databases and CSV exports there are local run products, not source.

## UI

`streamlit run src/ui/streamlit_app.py`

The page reads the snapshot database. The main table is the latest scan's top 100 by score. Display times use `Europe/Athens`.

`Rank`, `Movement`, `Symbol`, `Price`, `Status`, `Score`, `RSI`, `MACD Histogram`, `ATR%`, `Volume Ratio`, `ADV`

`ATR%` is `atr_percent * 100` (percent points). Status is `PASS` or `BLOCKED`. Raw veto codes stay in the database and are not shown in the Status column. BLOCKED symbols with a score appear in the top 100 when their rank is 100 or better. Symbols without a score are omitted from the table.

Movement compares the current rank with the symbol's previous stored rank (`previous_rank - current_rank`). An improvement is `🟢 ↑N`, a drop is `🔴 ↓N`, an unchanged rank is `—`, and no earlier rank is `NEW`.

A symbol selector under the table loads history for that symbol only. The range is `24H` (default), `3D`, or `5D`, measured back from the clock hour that contains the latest snapshot. The history table shows time, `PASS` or `BLOCKED`, and a readable reason (`—` for PASS; veto codes joined with `; ` for BLOCKED). Charts for RSI, Score, ATR%, and Volume Ratio use those stored rows. Coverage compares stored rows with that hourly grid. A latest snapshot at `17:02` fills the `17:00` hour, so it is not reported as a missing `:00` row, and the earlier hourly snapshots stay on their own timestamps. When the latest scan's earnings checks are all `unavailable`, the page says the earnings veto is pending.

The FastAPI app in `src/ui/app.py` remains a JSON view of the scores CSV.

## How to run

From the repository root, with the project virtualenv:

```text
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\run_universe.py --symbols AAPL,MSFT,NVDA,AMZN,META,JPM,XOM,JNJ
.venv\Scripts\streamlit run src/ui/streamlit_app.py
```

With no `--symbols`, the scan evaluates the full S&P 500 constituent list. `--size N` or `UNIVERSE_SIZE` caps that list in constituent order and does not sort by market cap. `UNIVERSE_DB` overrides the snapshot database. `MARKET_BARS_DB` overrides the OHLCV cache. `UNIVERSE_SYMBOLS` overrides the scheduler's symbol list.

## Known limitations

- Earnings dates are not available from the current provider. The veto interface exists and the check is reported as `unavailable`.
- RSI is Cutler's SMA form, matching the existing helper, not Wilder's RSI.
- ATR uses Wilder's recursive average seeded on the first true range, not an SMA-seeded ATR.
- ADV20 is daily dollar volume. VolumeRatio is hourly.
- Score caps (10% above EMA200, ±2% MACD, VolumeRatio 2.0, 20× ADV) only keep the weighted score on a common 0–10 scale. They are not optimized entry rules.
- Production bars come from Alpaca SIP with `adjustment=all`. `YFinanceMarketDataProvider` remains for tests and the offline Yahoo comparison. The universe does not add a second adjustment on top of the provider response.
- An existing `artifacts/market_bars.db` filled by an earlier Yahoo scan is not deleted by the provider switch. BarStore still skips a window it already considers fresh, and a material price change rebuilds only through the existing revision path.
- `observed_minutes` cannot be recovered from an hourly bar. Early pre-market and late after-hours hours often contain fewer than 60 trade minutes. That is recorded as coverage, not treated as a data failure.
- `REGULAR` hourly bars do not contain the 09:30 open or the 16:00 closing print. Those prints sit in the two `MIXED` hours.
- One pipeline run backfills the last 24 hourly bars already on disk. After that, new hours append when the scan runs again. The UI ranges `24H`, `3D`, and `5D` read those stored snapshots. A cash session is shorter than a calendar day, and a weekend has no new bars, so a range can contain fewer points than its label suggests.
- Rank movement uses the latest earlier snapshot that has a rank for that symbol. The first ranked snapshot for a symbol is `NEW`.
- The evaluation pool is the Wikipedia S&P 500 list, not a market-cap top 100. Top 100 is the score ranking shown in the UI.
- Half-days with an early official close are not a separate session type. The clock labels still apply.
- `scripts/run_backtest.py`, `scripts/report_backtest.py`, and `scripts/report_equity.py` are outside this milestone.

## Pre-existing modules outside this milestone

These files are still in the repo. Universe v1 does not call them to trade.

- `src/scoring/engine.py` — legacy raw weighted sum (`config/weights.yaml`).
- `src/bot/manager.py` — single-position paper bot (limit entry, 2% stop, 4% target, 24-hour hold).
- `src/execution/alpaca_adapter.py`, `src/risk/manager.py` — paper orders and position/loss limits.
- `src/backtest/engine.py`, `src/backtest/execution.py` — single-name and portfolio backtests with a local fill simulator.
- `src/strategy/simple_mean_reversion.py` — sample strategy used by the backtest scripts.
- `src/ui/app.py` — FastAPI `/scores`.
- `src/persistence/store.py` also creates a legacy `trades` table for that paper path. Universe snapshots are a separate table.

## Not implemented

- Using `coverage_ratio` or any enrichment field as a veto, a score weight, or a trade filter
- A reliable earnings calendar, news sentiment, and short interest
- Cross-sectional z-score scoring
- Order execution guard, position guardian, dynamic take-profit and stop-loss
- Multi-position portfolio selection, adaptive thresholds, market-regime detection
- Live trading

The legacy raw scorer is not the Universe v1 score.

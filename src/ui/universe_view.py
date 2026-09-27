"""Display frames for the Universe v1 table and history charts."""

from typing import Mapping, Sequence

import pandas as pd

DISPLAY_COLUMNS = [
    "Rank",
    "Symbol",
    "Price",
    "Status",
    "Score",
    "RSI",
    "MACD Histogram",
    "ATR %",
    "Volume Ratio",
    "ADV",
]

CHART_COLUMNS = ["RSI", "Score", "ATR %", "Volume Ratio"]


def _blank(value):
    if value is None or pd.isna(value):
        return pd.NA
    return value


def latest_table(rows: Sequence[Mapping]) -> pd.DataFrame:
    """One row per symbol for the latest scan, in ranking order."""
    frame = pd.DataFrame(
        {
            "Rank": [pd.NA if row.get("rank") is None else int(row["rank"]) for row in rows],
            "Symbol": [row.get("symbol") for row in rows],
            "Price": [_blank(row.get("hourly_close")) for row in rows],
            "Status": [row.get("veto_status") for row in rows],
            "Score": [_blank(row.get("final_score")) for row in rows],
            "RSI": [_blank(row.get("rsi14")) for row in rows],
            "MACD Histogram": [_blank(row.get("macd_hist")) for row in rows],
            "ATR %": [
                pd.NA if row.get("atr_percent") is None else float(row["atr_percent"]) * 100.0
                for row in rows
            ],
            "Volume Ratio": [_blank(row.get("volume_ratio")) for row in rows],
            "ADV": [_blank(row.get("adv20")) for row in rows],
        }
    )
    return frame.loc[:, DISPLAY_COLUMNS]


def history_chart_frame(rows: Sequence[Mapping]) -> pd.DataFrame:
    """Chart frame indexed by snapshot time. ATR % is shown in percent points."""
    if not rows:
        return pd.DataFrame(columns=CHART_COLUMNS)
    frame = pd.DataFrame(
        {
            "RSI": [row.get("rsi14") for row in rows],
            "Score": [row.get("final_score") for row in rows],
            "ATR %": [
                None if row.get("atr_percent") is None else float(row["atr_percent"]) * 100.0
                for row in rows
            ],
            "Volume Ratio": [row.get("volume_ratio") for row in rows],
        }
    )
    frame.index = pd.to_datetime([row["scan_timestamp"] for row in rows], utc=True)
    frame.index.name = "scan_timestamp"
    return frame.loc[:, CHART_COLUMNS]

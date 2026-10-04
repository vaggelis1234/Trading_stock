"""Local Universe v1 viewer.

Run from the repository root:

    streamlit run src/ui/streamlit_app.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.persistence.store import load_latest_scan, load_previous_ranks, load_symbol_history
from src.ui.universe_view import (
    DEFAULT_HISTORY_RANGE,
    HISTORY_RANGES,
    format_coverage_timestamps,
    format_local_timestamp,
    format_metric_coverage_summary,
    history_chart_frame,
    history_start,
    history_status_table,
    latest_table,
    metric_coverage,
    metric_snapshot_options,
    selected_ranking_symbol,
    symbol_detail_lines,
)
from src.universe.ranking import TOP_N


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise SystemExit("streamlit is required to view Universe v1") from exc

    st.set_page_config(page_title="Universe v1", layout="wide")
    st.title("Universe v1")
    st.caption("Analytical scan. Top 100 is the highest scores. Rank is not an order.")

    default_db = os.getenv("UNIVERSE_DB", os.path.join("artifacts", "universe.db"))
    db_path = st.sidebar.text_input("Snapshot database", default_db)
    if st.sidebar.button("Reload"):
        st.rerun()

    if not os.path.exists(db_path):
        st.warning("No snapshots yet. Run `python scripts/run_universe.py --symbols AAPL,MSFT`.")
        return

    rows = load_latest_scan(db_path)
    if not rows:
        st.warning("The snapshot database has no scans yet.")
        return

    earnings_checks = {row.get("earnings_check") for row in rows}
    if earnings_checks == {"unavailable"}:
        st.info(
            "Earnings hard veto is pending. Reliable earnings dates are not available "
            "from the current data provider, so this scan does not apply BLOCKED_EARNINGS."
        )

    passed = sum(1 for row in rows if row.get("veto_status") == "PASS")
    scored = sum(1 for row in rows if row.get("final_score") is not None)
    blocked = len(rows) - passed
    previous_ranks = load_previous_ranks(db_path, rows[0]["scan_timestamp"])
    table = latest_table(rows, previous_ranks, limit=TOP_N)
    executed_at = rows[0].get("executed_at")
    executed_label = format_local_timestamp(executed_at) if executed_at else "not recorded"
    st.write(
        f"Market data timestamp `{format_local_timestamp(rows[0]['scan_timestamp'])}` — "
        f"Scan executed `{executed_label}` — "
        f"{len(rows)} evaluated, {scored} scored, {passed} passed, {blocked} blocked. "
        f"Showing the top {len(table)} by score."
    )
    st.caption("PASS and BLOCKED symbols are ranked together. Status is separate from the score.")
    selection = st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="universe_ranking",
        column_config={
            "Price": st.column_config.NumberColumn(format="%.2f"),
            "Score": st.column_config.NumberColumn(format="%.2f"),
            "RSI": st.column_config.NumberColumn(format="%.1f"),
            "MACD Histogram": st.column_config.NumberColumn(format="%.4f"),
            "ATR%": st.column_config.NumberColumn(format="%.2f"),
            "Volume Ratio": st.column_config.NumberColumn(format="%.2f"),
            "ADV": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    selected = selected_ranking_symbol(table, selection)
    if not selected:
        st.info("No scored symbols in the latest scan.")
        return

    current = next(row for row in rows if row["symbol"] == selected)
    st.subheader(selected)
    for line in symbol_detail_lines(current, previous_ranks.get(selected)):
        st.write(line)

    range_labels = list(HISTORY_RANGES)
    choice = st.radio(
        "History",
        range_labels,
        horizontal=True,
        index=range_labels.index(DEFAULT_HISTORY_RANGE),
    )
    since = history_start(current["scan_timestamp"], choice)
    history = load_symbol_history(db_path, selected, since=since)
    st.caption(
        f"Stored snapshots from {format_local_timestamp(since)} through this scan. "
        "No new market data is fetched for history."
    )
    st.dataframe(history_status_table(history), width="stretch", hide_index=True)

    blocked_hours = sum(1 for row in history if row.get("veto_status") != "PASS")
    if blocked_hours:
        noun = "snapshot" if blocked_hours == 1 else "snapshots"
        st.caption(
            f"{blocked_hours} blocked {noun} in this range. "
            "Those hours stay on the charts and are labeled in the table."
        )

    chart = history_chart_frame(history)
    left, right = st.columns(2)
    panels = [("RSI", left), ("Score", right), ("ATR%", left), ("Volume Ratio", right)]
    anchor = current["scan_timestamp"]
    for name, column in panels:
        with column:
            st.markdown(f"**{name}**")
            options = metric_snapshot_options(history, name)
            if options:
                st.selectbox(
                    f"{name} snapshots",
                    options,
                    index=len(options) - 1,
                    key=f"metric_history_{selected}_{choice}_{name}",
                )
            coverage = metric_coverage(history, name, anchor, choice)
            st.caption(format_metric_coverage_summary(coverage, name))
            if coverage["missing"] or coverage["null_metric"]:
                with st.expander("Coverage"):
                    if coverage["missing"]:
                        st.caption("Missing snapshots: " + format_coverage_timestamps(coverage["missing"]))
                    if coverage["null_metric"]:
                        st.caption(
                            f"Snapshot present, {name} is null: "
                            + format_coverage_timestamps(coverage["null_metric"])
                        )
            if name not in chart.columns or chart[name].dropna().empty:
                st.caption("No values in this window.")
            else:
                st.line_chart(chart[[name]], width="stretch")


if __name__ == "__main__":
    main()

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

from src.persistence.store import load_latest_scan, load_symbol_history
from src.ui.universe_view import history_chart_frame, latest_table


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise SystemExit("streamlit is required to view Universe v1") from exc

    st.set_page_config(page_title="Universe v1", layout="wide")
    st.title("Universe v1")
    st.caption("Analytical scan of the stock universe. Ranking is not an order.")

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

    passed = sum(1 for row in rows if row["veto_status"] == "PASS")
    st.write(
        f"Latest scan `{rows[0]['scan_timestamp']}` — {passed} passed, {len(rows) - passed} blocked."
    )
    st.caption("Passing symbols are ranked from 1. Blocked symbols stay in the table with an empty rank and an empty score.")
    st.dataframe(latest_table(rows), width="stretch", hide_index=True)

    symbols = [row["symbol"] for row in rows]
    selected = st.selectbox("Symbol", symbols)
    history = load_symbol_history(db_path, selected, limit=24)
    current = history[-1] if history else next(row for row in rows if row["symbol"] == selected)
    reasons = current.get("veto_reasons") or []

    st.subheader(selected)
    st.write(f"Veto status: `{current.get('veto_status')}`")
    st.write("Veto reasons: " + (", ".join(reasons) if reasons else "none"))
    st.write(f"Earnings check: `{current.get('earnings_check')}`")
    st.caption(
        "Charts show the most recent 24 hourly snapshots for this symbol. "
        "Each point is one evaluated market hour. A missing score is an hour that failed a hard veto."
    )
    st.dataframe(
        [
            {
                "Time": row["scan_timestamp"],
                "Status": row["veto_status"],
                "Reasons": ", ".join(row.get("veto_reasons") or []) or "none",
            }
            for row in history
        ],
        width="stretch",
        hide_index=True,
    )

    chart = history_chart_frame(history)
    left, right = st.columns(2)
    panels = [("RSI", left), ("Score", right), ("ATR %", left), ("Volume Ratio", right)]
    for name, column in panels:
        with column:
            st.markdown(f"**{name}**")
            if name not in chart.columns or chart[name].dropna().empty:
                st.caption("No values in this window.")
            else:
                st.line_chart(chart[[name]], width="stretch")


if __name__ == "__main__":
    main()

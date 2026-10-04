from src.universe.minute_candidates import (
    MAX_CANDIDATES_FOR_1M,
    max_candidates_for_1m,
    select_minute_candidates,
)
from src.universe.ranking import rank_snapshots


def _row(symbol, status, score):
    return {"symbol": symbol, "veto_status": status, "final_score": score}


def test_configured_cap_is_ten_named_constant():
    assert MAX_CANDIDATES_FOR_1M == 10
    assert max_candidates_for_1m() == 10


def test_at_most_ten_pass_rows_are_selected_in_rank_order():
    rows = [_row(f"P{index:02d}", "PASS", float(index)) for index in range(15)]
    rows.append(_row("BLOCKED_TOP", "BLOCKED_RSI", 1000.0))
    rows.append(_row("UNSCORED", "PASS", None))
    ranked = rank_snapshots(rows)

    selected = select_minute_candidates(ranked)

    assert [row["symbol"] for row in selected] == [f"P{index:02d}" for index in range(14, 4, -1)]
    assert len(selected) == 10
    assert all(row["veto_status"] == "PASS" for row in selected)
    assert [row["rank"] for row in selected] == list(range(2, 12))
    assert all(row["final_score"] == ranked_score(ranked, row["symbol"]) for row in selected)


def ranked_score(ranked, symbol):
    return next(row["final_score"] for row in ranked if row["symbol"] == symbol)


def test_fewer_than_ten_eligible_rows_use_all_of_them():
    ranked = rank_snapshots(
        [
            _row("AAA", "BLOCKED_VOLUME", 9.0),
            _row("BBB", "PASS", 3.0),
            _row("CCC", "PASS", 8.0),
            _row("DDD", "BLOCKED_TREND", 7.0),
        ]
    )
    selected = select_minute_candidates(ranked)
    assert [row["symbol"] for row in selected] == ["CCC", "BBB"]


def test_no_eligible_candidate_selects_nobody():
    ranked = rank_snapshots(
        [
            _row("AAA", "BLOCKED_RSI", 9.0),
            _row("BBB", "PASS", None),
            _row("CCC", "BLOCKED_INSUFFICIENT_DATA", None),
        ]
    )
    assert select_minute_candidates(ranked) == []


def test_unranked_rows_are_not_eligible():
    pending = [_row("AAA", "PASS", 9.0), _row("BBB", "PASS", 8.0)]
    assert select_minute_candidates(pending) == []

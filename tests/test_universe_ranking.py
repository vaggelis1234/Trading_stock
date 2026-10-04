from src.universe.ranking import TOP_N, rank_snapshots


def _row(symbol, status, score):
    return {
        "symbol": symbol,
        "veto_status": status,
        "final_score": score,
        "trend_score": score,
        "momentum_score": score,
        "volatility_score": score,
        "volume_liquidity_score": score,
    }


def test_blocked_and_pass_symbols_rank_together_by_score():
    ranked = rank_snapshots(
        [
            _row("AAA", "BLOCKED_RSI", 9.12),
            _row("ZZZ", "PASS", 8.97),
            _row("DEF", "BLOCKED_VOLUME", 8.85),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["AAA", "ZZZ", "DEF"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]
    assert [row["veto_status"] for row in ranked] == ["BLOCKED_RSI", "PASS", "BLOCKED_VOLUME"]
    assert ranked[0]["final_score"] == 9.12
    assert ranked[0]["trend_score"] == 9.12


def test_symbols_rank_by_score_then_symbol():
    ranked = rank_snapshots(
        [
            _row("MMM", "PASS", 5.0),
            _row("AAA", "BLOCKED_TREND", 5.0),
            _row("ZZZ", "PASS", 7.0),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["ZZZ", "AAA", "MMM"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]


def test_missing_score_has_no_rank_and_follows_scored_rows():
    ranked = rank_snapshots(
        [
            _row("BBB", "BLOCKED_INSUFFICIENT_DATA", None),
            _row("AAA", "PASS", None),
            _row("CCC", "BLOCKED_VOLUME", 3.0),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["CCC", "AAA", "BBB"]
    assert ranked[0]["rank"] == 1
    assert ranked[0]["final_score"] == 3.0
    assert ranked[1]["rank"] is None
    assert ranked[1]["final_score"] is None
    assert ranked[2]["rank"] is None


def test_top_100_is_score_rank_not_pass_status():
    rows = [_row(f"P{index:03d}", "PASS", float(index)) for index in range(TOP_N)]
    rows.append(_row("HIGH", "BLOCKED_VOLUME", 1000.0))
    ranked = rank_snapshots(rows)
    by_symbol = {row["symbol"]: row for row in ranked}
    assert by_symbol["HIGH"]["rank"] == 1
    assert by_symbol["HIGH"]["veto_status"] == "BLOCKED_VOLUME"
    top = [row for row in ranked if row["rank"] is not None and row["rank"] <= TOP_N]
    assert len(top) == TOP_N
    assert any(row["veto_status"] != "PASS" for row in top)
    assert by_symbol["P000"]["rank"] == TOP_N + 1
    assert all(row["final_score"] is not None for row in top)

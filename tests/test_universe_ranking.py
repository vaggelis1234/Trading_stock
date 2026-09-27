from src.universe.ranking import rank_snapshots


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


def test_blocked_symbol_cannot_rank_above_a_passing_symbol():
    ranked = rank_snapshots(
        [
            _row("AAA", "BLOCKED_RSI", 99.0),
            _row("ZZZ", "PASS", 1.0),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["ZZZ", "AAA"]
    assert ranked[0]["rank"] == 1
    assert ranked[0]["final_score"] == 1.0
    assert ranked[1]["rank"] is None
    assert ranked[1]["final_score"] is None
    assert ranked[1]["trend_score"] is None


def test_passing_symbols_rank_by_score_then_symbol():
    ranked = rank_snapshots(
        [
            _row("MMM", "PASS", 5.0),
            _row("AAA", "PASS", 5.0),
            _row("ZZZ", "PASS", 7.0),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["ZZZ", "AAA", "MMM"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]


def test_blocked_rows_remain_visible_after_the_candidates():
    ranked = rank_snapshots(
        [
            _row("BBB", "BLOCKED_VOLUME", None),
            _row("AAA", "BLOCKED_TREND", None),
            _row("CCC", "PASS", 3.0),
        ]
    )
    assert [row["symbol"] for row in ranked] == ["CCC", "AAA", "BBB"]
    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] is None
    assert ranked[2]["rank"] is None

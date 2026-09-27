"""Candidate ranking for Universe v1.

Only PASS rows with a final score receive ranks 1..N (1 is the highest score).
Blocked rows stay in the result, with rank and score cleared, after every
passing row. A blocked score cannot outrank a passing symbol.
"""

from typing import Mapping, Sequence

from src.universe.scoring import SCORE_FIELDS


def rank_snapshots(rows: Sequence[Mapping]) -> list[dict]:
    ranked = [dict(row) for row in rows]
    for row in ranked:
        if row.get("veto_status") != "PASS":
            for field in SCORE_FIELDS:
                row[field] = None
            row["rank"] = None

    passing = []
    blocked = []
    for row in ranked:
        if row.get("veto_status") == "PASS" and row.get("final_score") is not None:
            passing.append(row)
        else:
            blocked.append(row)
    passing.sort(key=lambda row: (-float(row["final_score"]), str(row["symbol"])))
    for index, row in enumerate(passing, start=1):
        row["rank"] = index
    for row in blocked:
        row["rank"] = None
    blocked.sort(key=lambda row: str(row.get("symbol", "")))
    return passing + blocked

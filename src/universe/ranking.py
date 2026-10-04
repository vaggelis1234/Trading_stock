"""Candidate ranking for Universe v1.

Every row with a finite final score receives a rank. Rank 1 is the highest
score. PASS and BLOCKED rows are ranked together. Rows without a score keep
an empty rank and are listed after the ranked rows.

Top 100 is ``rank <= TOP_N``. This function ranks the full scored set.
"""

import math
from typing import Mapping, Optional, Sequence

TOP_N = 100


def _score_value(row: Mapping) -> Optional[float]:
    value = row.get("final_score")
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def rank_snapshots(rows: Sequence[Mapping]) -> list[dict]:
    ranked = [dict(row) for row in rows]
    scored = []
    unscored = []
    for row in ranked:
        score = _score_value(row)
        if score is None:
            row["final_score"] = None
            row["rank"] = None
            unscored.append(row)
        else:
            row["final_score"] = score
            scored.append(row)
    scored.sort(key=lambda row: (-float(row["final_score"]), str(row.get("symbol", ""))))
    for index, row in enumerate(scored, start=1):
        row["rank"] = index
    unscored.sort(key=lambda row: str(row.get("symbol", "")))
    return scored + unscored

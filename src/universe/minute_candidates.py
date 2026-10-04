"""Who may receive a 1-minute SIP lookup after the hourly ranking.

The hourly scan still scores every symbol from SIP 1h bars. Ranking and the
hard veto stay the broad screen. This module only chooses the PASS rows a
later step is allowed to enrich with 1-minute bars. It does not fetch bars,
change scores, or add a coverage veto.
"""

from pathlib import Path
from typing import Mapping, Optional, Sequence

import yaml

from src.universe.veto import PASS

MAX_CANDIDATES_FOR_1M = 10


def default_universe_config_path() -> Path:
    cwd = Path("config") / "universe.yaml"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parents[2] / "config" / "universe.yaml"


def max_candidates_for_1m(path: Optional[str] = None) -> int:
    """Configured cap for post-ranking 1-minute enrichment."""
    config_path = Path(path) if path else default_universe_config_path()
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if "max_candidates_for_1m" in loaded:
            return _cap(loaded["max_candidates_for_1m"])
    return MAX_CANDIDATES_FOR_1M


def select_minute_candidates(
    ranked: Sequence[Mapping],
    limit: Optional[int] = None,
) -> list[dict]:
    """Return up to ``limit`` PASS rows, best rank first.

    ``ranked`` must already be the output of ``rank_snapshots``. Rows without
    a rank, and every non-PASS row, are not eligible. The default limit is
    ``max_candidates_for_1m``. An empty result means no 1-minute request.
    """
    cap = MAX_CANDIDATES_FOR_1M if limit is None else _cap(limit)
    eligible = [dict(row) for row in ranked if _eligible(row)]
    eligible.sort(key=lambda row: (int(row["rank"]), str(row.get("symbol", ""))))
    return eligible[:cap]


def _cap(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"max_candidates_for_1m must be a non-negative integer, got {value!r}")
    if value < 0:
        raise ValueError(f"max_candidates_for_1m must be a non-negative integer, got {value!r}")
    return value


def _eligible(row: Mapping) -> bool:
    if row.get("veto_status") != PASS:
        return False
    rank = row.get("rank")
    return isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1

"""Read-only Universe routes on the existing FastAPI application.

Bind the server to localhost. These routes do not write, scan, or trade.
"""

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from src.ui.api.db import universe_db_path
from src.ui.api.read_models import NoSnapshots, SymbolNotFound, latest_scan_payload, search_symbols, symbol_payload
from src.ui.api.rules import rules_payload
from src.ui.universe_view import HISTORY_RANGES

router = APIRouter(prefix="/api/v1", tags=["universe"])

_RANGES = tuple(HISTORY_RANGES)


def _no_snapshots() -> JSONResponse:
    return JSONResponse(content={"error": "no_snapshots"}, status_code=404)


@router.get("/rules")
def get_rules():
    return rules_payload()


@router.get("/scan/latest")
def get_latest_scan():
    try:
        return latest_scan_payload(universe_db_path())
    except FileNotFoundError:
        return _no_snapshots()
    except NoSnapshots:
        return _no_snapshots()


@router.get("/symbols")
def get_symbols(
    q: Optional[str] = Query(default=None, max_length=32),
    status: Optional[str] = Query(default=None, pattern="^(PASS|BLOCKED)$"),
    min_score: Optional[float] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    text = q.strip() if q else None
    if text == "":
        text = None
    try:
        return search_symbols(
            universe_db_path(),
            q=text,
            status=status,
            min_score=min_score,
            limit=limit,
            offset=offset,
        )
    except FileNotFoundError:
        return _no_snapshots()
    except NoSnapshots:
        return _no_snapshots()


@router.get("/symbols/{symbol}")
def get_symbol(
    symbol: str,
    range_key: str = Query(default="24H", alias="range", pattern="^(24H|3D|5D)$"),
):
    if range_key not in _RANGES:
        return JSONResponse(content={"error": "unknown_range"}, status_code=422)
    try:
        return symbol_payload(universe_db_path(), symbol, range_key)
    except FileNotFoundError:
        return _no_snapshots()
    except NoSnapshots:
        return _no_snapshots()
    except SymbolNotFound:
        return JSONResponse(content={"error": "symbol_not_found"}, status_code=404)

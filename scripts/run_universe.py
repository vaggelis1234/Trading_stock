"""Run one Universe v1 scan and append hourly snapshots.

Examples:

    python scripts/run_universe.py --symbols AAPL,MSFT,NVDA
    python scripts/run_universe.py --size 100
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.scan import build_universe_top_sp500
from src.logging_config import setup_logging
from src.universe.pipeline import run_universe


def resolve_symbols(raw_symbols: str, size: int | None) -> list[str]:
    if raw_symbols.strip():
        return [part.strip().upper() for part in raw_symbols.split(",") if part.strip()]
    universe_size = size if size is not None else int(os.getenv("UNIVERSE_SIZE", "100"))
    return build_universe_top_sp500(n=universe_size)


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run the Universe v1 scan and persist snapshots.")
    parser.add_argument("--symbols", default=os.getenv("UNIVERSE_SYMBOLS", ""), help="Comma-separated tickers. Skips the S&P market-cap sort.")
    parser.add_argument("--size", type=int, default=None, help="Top N of the S&P 500 universe when --symbols is omitted.")
    parser.add_argument("--lookback", type=int, default=24, help="Hourly bars to snapshot, counting backward from the latest bar.")
    parser.add_argument("--db", default=os.getenv("UNIVERSE_DB", os.path.join("artifacts", "universe.db")))
    parser.add_argument("--bars-db", default=os.getenv("MARKET_BARS_DB", os.path.join("artifacts", "market_bars.db")))
    parser.add_argument("--artifact-dir", default=os.getenv("ARTIFACT_DIR", "artifacts"))
    args = parser.parse_args(argv)

    symbols = resolve_symbols(args.symbols, args.size)
    rows = run_universe(
        symbols,
        db_path=args.db,
        bars_db=args.bars_db,
        artifact_dir=args.artifact_dir,
        lookback_bars=args.lookback,
    )
    print(f"symbols={len(symbols)} latest_rows={len(rows)} db={args.db}")
    for row in rows:
        rank = "-" if row.get("rank") is None else str(row["rank"])
        score = "" if row.get("final_score") is None else f"{row['final_score']:.2f}"
        reasons = ",".join(row.get("veto_reasons") or [])
        price = row.get("hourly_close")
        price_text = "" if price is None else f"{price:.2f}"
        print(f"{rank:>4}  {row['symbol']:<8} {price_text:>10}  {row['veto_status']:<28} {score:>6}  {reasons}")


if __name__ == "__main__":
    main()

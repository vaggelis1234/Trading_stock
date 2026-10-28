"""Open the Trading Bot desktop window.

Uses the repository virtualenv when launched from the desktop shortcut.
The working directory must be the repository root so artifacts and config resolve.
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.desktop.app import TradingBotApp
from src.logging_config import setup_logging


def main() -> None:
    setup_logging()
    log_path = ROOT / "artifacts" / "control.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        TradingBotApp().run()
    except Exception:
        logging.exception("Trading Bot window failed")
        raise


if __name__ == "__main__":
    main()

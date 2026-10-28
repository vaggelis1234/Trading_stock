"""CLI for the same Trading Bot process the desktop window starts."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.control.cli import parse_args
from src.control.instance import AlreadyRunning
from src.control.service import ControlService
from src.logging_config import setup_logging


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    options = parse_args(argv)
    service = ControlService(options)
    try:
        service.start()
        if service.loops_active:
            service.wait()
    except AlreadyRunning as exc:
        print(exc)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

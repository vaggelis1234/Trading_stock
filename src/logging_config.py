import logging
import sys


def setup_logging(level: int = logging.INFO):
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    handler.setFormatter(fmt)
    root.setLevel(level)
    root.addHandler(handler)

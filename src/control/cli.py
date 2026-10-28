"""Arguments for the Trading Bot control process.

The help text is this parser. Flags that are not defined here are not offered.
"""

from __future__ import annotations

import argparse

from src.control.permissions import LaunchOptions, resolve_buy_permission

PROG = "trading-bot"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Run one immediate Universe scan, then the selected stage-1 services. "
            "Buy execution is not implemented. --yes_buy only records approval."
        ),
    )
    parser.add_argument(
        "--enable_scheduler",
        action="store_true",
        help=(
            "After the initial scan, run Universe scans on the NYSE cash-session "
            "clock in America/New_York. The initial scan does not claim a slot."
        ),
    )
    parser.add_argument(
        "--enable_watch_engine",
        action="store_true",
        help=(
            "Schedule the existing Watch Engine once a minute. It evaluates a "
            "minute only while candidates or working orders need attention."
        ),
    )
    permission = parser.add_mutually_exclusive_group()
    permission.add_argument(
        "--no_buy",
        action="store_true",
        help="Safe default. Do not approve buy execution. This is used when --yes_buy is omitted.",
    )
    permission.add_argument(
        "--yes_buy",
        action="store_true",
        help=(
            "Record explicit user approval for stage-2 buy execution. "
            "Requires both --enable_scheduler and --enable_watch_engine. "
            "Does not define an entry rule, enable the risk engine, or submit orders. "
            "Trading execution stays unavailable until that stage is implemented."
        ),
    )
    return parser


def parse_args(argv: list[str] | None = None) -> LaunchOptions:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        permission = resolve_buy_permission(
            enable_scheduler=args.enable_scheduler,
            enable_watch_engine=args.enable_watch_engine,
            yes_buy=args.yes_buy,
        )
    except Exception as exc:
        parser.error(str(exc))
    return LaunchOptions(
        enable_scheduler=args.enable_scheduler,
        enable_watch_engine=args.enable_watch_engine,
        buy_permission=permission,
    )


def format_help() -> str:
    return build_parser().format_help()

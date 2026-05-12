#!/usr/bin/env python3
"""Populate us_symbol_universe.is_common_stock from public NASDAQ Trader files.

Defaults to dry-run. Pass --commit only after reviewer/operator approval.
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.cli import setup_logging_and_sentry
from app.jobs.us_common_stock_classifier import run_us_common_stock_flag_sync


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify active US universe rows as common stocks (dry-run by default)."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist is_common_stock changes. Default is --dry-run.",
    )
    args = parser.parse_args(argv)
    args.dry_run = not args.commit
    return args


def _print_result(result) -> None:
    print(
        "\nUS common-stock flag sync "
        f"(dry_run={not result.committed}, active={result.active_symbols}, "
        f"classified={result.classified_symbols}):"
    )
    print(f"  common_true:  {result.common_true}")
    print(f"  common_false: {result.common_false}")
    print(f"  changed:      {result.changed}")
    if result.committed:
        print("\ncommitted is_common_stock flag updates.\n")
    else:
        print("\n--dry-run: no rows written.\n")


async def run(args: argparse.Namespace) -> int:
    result = await run_us_common_stock_flag_sync(commit=args.commit)
    _print_result(result)
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="sync-us-common-stock-flags")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

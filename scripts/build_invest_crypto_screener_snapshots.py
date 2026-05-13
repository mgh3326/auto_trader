#!/usr/bin/env python3
"""Build invest_crypto_screener_snapshots rows from the Upbit crypto screener.

Defaults to dry-run/no DB writes.  Pass --commit only after operator approval and
reviewing dry-run evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.cli import setup_logging_and_sentry
from app.jobs.invest_crypto_screener_snapshots import (
    CryptoSnapshotBuildRequest,
    run_crypto_snapshot_build,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only crypto screener snapshot builder (ROB-225)."
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch the full provider result set instead of --limit rows.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.all and args.limit != 50:
        parser.error("--all is mutually exclusive with --limit")
    return args


async def run(args: argparse.Namespace) -> int:
    result = await run_crypto_snapshot_build(
        CryptoSnapshotBuildRequest(
            limit=args.limit,
            all_markets=args.all,
            commit=args.commit,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.commit:
        print("\n--dry-run: no rows written. Pass --commit only with operator approval.")
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="build-invest-crypto-screener-snapshots")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

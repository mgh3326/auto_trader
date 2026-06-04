#!/usr/bin/env python3
"""Build invest_kr_fundamentals_snapshots rows from the tvscreener KR screener.

Defaults to dry-run/no DB writes. Pass --commit only after operator approval
and reviewing dry-run evidence (ROB-428 PR-A). Uses the tvscreener library
scanner API only — kr.tradingview.com is never crawled.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.cli import setup_logging_and_sentry
from app.jobs.invest_kr_fundamentals_snapshots import (
    KrFundamentalsSnapshotBuildRequest,
    run_kr_fundamentals_snapshot_build,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only KR fundamentals screener snapshot builder (ROB-428)."
    )
    parser.add_argument("--limit", type=int, default=200)
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
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Override the ROB-429 coverage guard and commit a partial backfill "
            "(below 80%% of the active KR universe). Operator-gated."
        ),
    )
    args = parser.parse_args(argv)
    if args.all and args.limit != 200:
        parser.error("--all is mutually exclusive with --limit")
    return args


async def run(args: argparse.Namespace) -> int:
    result = await run_kr_fundamentals_snapshot_build(
        KrFundamentalsSnapshotBuildRequest(
            limit=args.limit,
            all_symbols=args.all,
            commit=args.commit,
            allow_partial=args.allow_partial,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    # ROB-429 A2: surface the coverage guard metadata for the operator packet.
    print(
        "\ncoverage: would_upsert={would_upsert} / active_universe={universe} "
        "= {ratio:.1%} (commit_allowed={allowed})".format(
            would_upsert=result.get("would_upsert"),
            universe=result.get("active_universe_count"),
            ratio=result.get("coverage_ratio") or 0.0,
            allowed=result.get("commit_allowed"),
        )
    )
    if result.get("block_reason"):
        print(f"blocked: {result['block_reason']}")
    if not args.commit:
        print(
            "\n--dry-run: no rows written. Pass --commit only with operator approval."
        )
    elif not result.get("committed"):
        print(
            "\n--commit was requested but BLOCKED by the coverage guard; no rows "
            "written. Pass --allow-partial to override (operator-gated)."
        )
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="build-invest-kr-fundamentals-snapshots")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

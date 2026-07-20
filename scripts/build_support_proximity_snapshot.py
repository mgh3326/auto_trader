#!/usr/bin/env python3
"""Build persisted support-proximity rows (bounded and dry-run by default).

The ordinary KR screener snapshot must exist first; it supplies the cheap
candidate proxy.  This command then fetches completed OHLCV only for the bounded
pool, freezes price/support/distance and normalized market cap together, and
writes exclusively through ``InvestScreenerSnapshotsRepository.upsert`` when
``--commit`` is explicitly supplied.

Examples:
    uv run python -m scripts.build_support_proximity_snapshot --market kr

    uv run python -m scripts.build_support_proximity_snapshot \
        --market kr --candidate-pool-limit 30 --commit
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from app.core.cli import setup_logging_and_sentry
from app.jobs import support_proximity_snapshots as snapshot_job


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded support_proximity snapshot builder. Defaults to dry-run; "
            "pass --commit to persist."
        )
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum ranked samples to print (default 10).",
    )
    parser.add_argument(
        "--candidate-pool-limit",
        type=int,
        default=snapshot_job.DEFAULT_CANDIDATE_POOL_LIMIT,
        help=(
            "Maximum symbols receiving a completed-OHLCV/support calculation "
            f"(default {snapshot_job.DEFAULT_CANDIDATE_POOL_LIMIT}, max "
            f"{snapshot_job.MAX_CANDIDATE_POOL_LIMIT})."
        ),
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--min-market-cap",
        type=Decimal,
        default=snapshot_job.DEFAULT_MIN_MARKET_CAP_KRW,
    )
    parser.add_argument(
        "--min-turnover",
        type=Decimal,
        default=snapshot_job.DEFAULT_MIN_TURNOVER_KRW,
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist through InvestScreenerSnapshotsRepository.upsert.",
    )
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if not 1 <= args.candidate_pool_limit <= snapshot_job.MAX_CANDIDATE_POOL_LIMIT:
        parser.error(
            "--candidate-pool-limit must be between 1 and "
            f"{snapshot_job.MAX_CANDIDATE_POOL_LIMIT}"
        )
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    args.dry_run = not args.commit
    return args


def _print_result(
    result: snapshot_job.SupportProximityBuildResult, *, limit: int
) -> None:
    print(
        f"\nsupport_proximity build (market={result.market}, "
        f"source_partition={result.source_partition_date}, "
        f"candidates={result.candidates_resolved}, "
        f"snapshots={result.snapshots_built}, supports={result.supports_built}):"
    )
    for row in result.samples[:limit]:
        print(
            f"  {row.symbol}: close={row.latest_close} "
            f"support={row.support_price} ({row.support_kind}, "
            f"{row.support_strength}) dist={row.dist_to_support_pct}% "
            f"market_cap={row.market_cap}"
        )
    for warning in result.warnings:
        print(f"warning: {warning}")
    if result.committed:
        print(f"\ncommitted {result.snapshots_built} row(s).\n")
    else:
        print("\n--dry-run: no rows written. Pass --commit to persist.\n")


async def run(args: argparse.Namespace) -> int:
    request = snapshot_job.SupportProximityBuildRequest(
        market=args.market,
        candidate_pool_limit=args.candidate_pool_limit,
        concurrency=args.concurrency,
        min_market_cap=args.min_market_cap,
        min_turnover=args.min_turnover,
        commit=args.commit,
    )
    result = await snapshot_job.run_support_proximity_build(request)
    _print_result(result, limit=args.limit)
    return 0 if result.source_partition_date is not None else 1


async def main() -> int:
    setup_logging_and_sentry(service_name="build-support-proximity-snapshot")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

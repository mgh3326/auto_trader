#!/usr/bin/env python3
"""Build KR investor_flow_snapshots rows from Naver Finance investor trends.

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without
committing to the database. Pass --commit only after explicit operator approval.

Examples:
    # bounded dry-run approval packet for first 20 active KR symbols
    uv run python -m scripts.build_investor_flow_snapshots --market kr --limit 20 --days 20

    # full-universe dry-run approval packet
    uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100

    # approved write only after operator approval
    uv run python -m scripts.build_investor_flow_snapshots --market kr --all --days 20 --batch-size 100 --commit
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

MAX_DAYS = 60


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first KR investor_flow_snapshots builder (ROB-205)."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to specific 6-digit KR symbols. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max active KR universe symbols to process. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Iterate the full active KR universe. Mutually exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=20,
        help=f"Naver investor-flow history days to fetch (1-{MAX_DAYS}, default 20).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Symbols per processing batch when --all is set (default 100).",
    )
    parser.add_argument("--concurrency", type=int, default=4, help="Per-symbol fetch concurrency.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is --dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    if args.days < 1 or args.days > MAX_DAYS:
        parser.error(f"--days must be between 1 and {MAX_DAYS}")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


async def _resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    """Compatibility shim around the shared job resolver."""
    from app.jobs import investor_flow_snapshots as snapshot_job

    return await snapshot_job.resolve_symbols(market, override, limit)


async def _resolve_active_universe(market: str) -> list[str]:
    """Compatibility shim around the shared job full-universe resolver."""
    from app.jobs import investor_flow_snapshots as snapshot_job

    return await snapshot_job.resolve_active_universe(market)


def _print_result(result: Any) -> None:
    print(
        f"\nbuilt {result.snapshots_built} investor-flow snapshots "
        f"for {result.symbols_resolved} KR symbols "
        f"(dry_run={not result.committed}, batches={result.batches}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    if result.snapshot_date_distribution:
        print("snapshot dates:")
        for snapshot_date, count in result.snapshot_date_distribution.items():
            print(f"  {snapshot_date}: {count}")
    if result.samples:
        print("samples:")
        for sample in result.samples[:10]:
            print(
                f"  {sample.market}:{sample.symbol} {sample.snapshot_date} "
                f"source={sample.source} foreign={sample.foreign_net} "
                f"institution={sample.institution_net} individual={sample.individual_net} "
                f"doubleBuy={sample.double_buy} doubleSell={sample.double_sell}"
            )
    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if not result.committed:
        print("\n--dry-run: no rows written.\n")
    else:
        print(f"\ncommitted {result.snapshots_built} rows.\n")


async def run(args: argparse.Namespace) -> int:
    from app.jobs import investor_flow_snapshots as snapshot_job

    result = await snapshot_job.run_investor_flow_snapshot_build(
        snapshot_job.InvestorFlowSnapshotBuildRequest(
            market=args.market,
            symbols=tuple(args.symbol),
            limit=args.limit,
            all_symbols=args.all,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            days=args.days,
            commit=args.commit,
        )
    )
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-investor-flow-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

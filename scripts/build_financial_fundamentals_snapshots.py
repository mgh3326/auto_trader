#!/usr/bin/env python3
"""Build financial_fundamentals_snapshots rows from DART (ROB-422 PR1, KR-only).

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without committing.
Pass --commit only after explicit operator approval. Production migration apply
(`alembic upgrade head`) and any scheduler activation remain operator-gated (spec §9).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first KR fundamentals snapshots builder (ROB-422 PR1)."
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument(
        "--symbol", action="append", default=[],
        help="Restrict to specific 6-digit KR symbols. Repeatable.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max active universe symbols. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Iterate the full active KR universe. Exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--with-quarterly", dest="include_quarterly", action="store_true",
        help="Also build quarterly periods (annual-only by default; spec §2.3).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the database. Default is --dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


def _print_result(result) -> None:
    print(
        f"\nbuilt {result.snapshots_built} fundamentals snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    if result.samples:
        print("samples:")
        for sample in result.samples[:10]:
            print(f"  {sample}")
    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if not result.committed:
        print("\n--dry-run: no rows written.\n")
    else:
        print(f"\ncommitted {result.snapshots_built} rows.\n")


async def run(args: argparse.Namespace) -> int:
    from app.jobs import financial_fundamentals_snapshots as snapshot_job

    result = await snapshot_job.run_financial_fundamentals_snapshot_build(
        snapshot_job.FinancialFundamentalsSnapshotBuildRequest(
            market=args.market,
            symbols=tuple(args.symbol),
            limit=args.limit,
            all_symbols=args.all,
            include_quarterly=args.include_quarterly,
            concurrency=args.concurrency,
            commit=args.commit,
        )
    )
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-financial-fundamentals-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

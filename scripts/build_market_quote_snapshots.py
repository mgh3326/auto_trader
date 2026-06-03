#!/usr/bin/env python3
"""Build market_quote_snapshots rows.

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without
committing to the database. Pass --commit only after explicit operator approval.
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first quote snapshots builder (ROB-206)."
    )
    parser.add_argument("--market", choices=["kr", "us", "crypto"], default="kr")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to specific symbols. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max active universe symbols to process. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Iterate the full active universe. Mutually exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Symbols per processing batch when --all is set.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is --dry-run/no writes.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial/thin build that is below the "
            "coverage floor (skips the commit guard). Use for small "
            "--symbol/--limit backfills."
        ),
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


def _print_result(result) -> None:
    print(
        f"\nbuilt {result.snapshots_built} quote snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}, batches={result.batches}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    distribution = getattr(result, "snapshot_at_distribution", {})
    if distribution:
        print("snapshot distribution:")
        for snapshot_key, count in distribution.items():
            print(f"  {snapshot_key}: {count}")
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
    from app.jobs import market_quote_snapshots as snapshot_job
    from app.services.snapshot_commit_guard import PartialCommitBlocked

    request = snapshot_job.MarketQuoteSnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        all_symbols=args.all,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
    )
    use_guarded = args.commit and not args.allow_partial
    try:
        if use_guarded:
            result = await snapshot_job.run_market_quote_snapshot_build_guarded(request)
        else:
            result = await snapshot_job.run_market_quote_snapshot_build(request)
    except PartialCommitBlocked as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-market-quote-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

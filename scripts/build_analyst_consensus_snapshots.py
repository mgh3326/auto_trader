#!/usr/bin/env python3
"""Build analyst_consensus_snapshots rows (ROB-641).

DEFAULTS TO --dry-run: prints an approval-packet-friendly summary without
committing to the database. Pass --commit only after explicit operator approval.

Default symbol scope is holdings ∪ active watch symbols (KIS holdings +
manual holdings + active investment_watch_alerts). There is intentionally no
full-universe option — consensus fetches are expensive per symbol. Use
--symbol for explicit overrides.
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first analyst consensus snapshots builder (ROB-641)."
    )
    parser.add_argument("--market", choices=["kr", "us"], default="kr")
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
        help=(
            "Cap the resolved holdings∪watch symbol count. "
            "Defaults to no cap (the scope is already bounded)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Symbols per processing batch.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is --dry-run/no writes.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    args.dry_run = not args.commit
    return args


def _print_result(result) -> None:
    print(
        f"\nbuilt {result.snapshots_built} consensus snapshots "
        f"for {result.symbols_resolved} {result.market.upper()} symbols "
        f"(dry_run={not result.committed}, batches={result.batches}):"
    )
    print("idempotency:")
    for key in ("wouldInsert", "wouldUpdate", "duplicatePayloadKeys"):
        print(f"  {key}: {result.idempotency.get(key, 0)}")
    distribution = getattr(result, "snapshot_date_distribution", {})
    if distribution:
        print("snapshot date distribution:")
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
    from app.jobs import analyst_consensus_snapshots as snapshot_job

    request = snapshot_job.AnalystConsensusSnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
    )
    result = await snapshot_job.run_analyst_consensus_snapshot_build(request)
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-analyst-consensus-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

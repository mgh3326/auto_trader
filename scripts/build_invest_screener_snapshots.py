#!/usr/bin/env python3
"""Build invest_screener_snapshots rows for an active KR or US universe slice.

DEFAULTS TO --dry-run: prints the SnapshotBuildResult summary it would write,
without committing to the database. Pass --commit to actually persist.

Examples:
    # KR, dry-run, top 20 active universe symbols
    uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

    # KR, full active universe, dry-run (RECOMMENDED before any --commit)
    uv run python -m scripts.build_invest_screener_snapshots --market kr --all

    # KR, full active universe, persist (REQUIRES OPERATOR APPROVAL)
    uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

    # US explicit symbols, persist
    uv run python -m scripts.build_invest_screener_snapshots \
        --market us --symbol AAPL --symbol MSFT --commit

    # US common-stock universe only, dry-run
    uv run python -m scripts.build_invest_screener_snapshots \
        --market us --all --common-stocks-only
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.cli import setup_logging_and_sentry
from app.jobs import invest_screener_snapshots as snapshot_job


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-mostly invest_screener_snapshots builder (ROB-170)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
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
        help=(
            "Iterate the full active universe in --batch-size chunks. Mutually "
            "exclusive with --symbol/--limit."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Symbols per processing batch when --all is set (default 200).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write to the database. Default is --dry-run.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Acknowledge and commit a partial/thin screener build, bypassing the "
            "row-count + dominant-partition guards (for small --symbol/--limit "
            "backfills)."
        ),
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--common-stocks-only",
        action="store_true",
        help=(
            "US only: restrict universe resolution to active rows with "
            "us_symbol_universe.is_common_stock IS TRUE."
        ),
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.common_stocks_only and args.market != "us":
        parser.error("--common-stocks-only is only supported with --market us")
    if args.limit is None:
        args.limit = 20
    args.dry_run = not args.commit
    return args


async def _resolve_symbols(
    market: str, override: list[str], limit: int, *, common_stocks_only: bool = False
) -> list[str]:
    """Compatibility shim around the shared job resolver."""
    return await snapshot_job.resolve_symbols(
        market, override, limit, common_stocks_only=common_stocks_only
    )


async def _resolve_active_universe(
    market: str, *, common_stocks_only: bool = False
) -> list[str]:
    """Compatibility shim around the shared job full-universe resolver."""
    return await snapshot_job.resolve_active_universe(
        market, common_stocks_only=common_stocks_only
    )


def _print_result(result) -> None:
    print(
        f"\nbuilt {result.snapshots_built}/{result.symbols_resolved} snapshots "
        f"(market={result.market}, dry_run={not result.committed}, "
        f"batches={result.batches}):"
    )
    if result.snapshot_date_distribution:
        print("snapshot dates:")
        for snapshot_date, count in result.snapshot_date_distribution.items():
            print(f"  {snapshot_date}: {count}")
    for sample in result.samples[:10]:
        print(
            f"  {sample.market}:{sample.symbol} {sample.snapshot_date} "
            f"close={sample.latest_close} streak={sample.consecutive_up_days} "
            f"week={sample.week_change_rate}"
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
    from app.services.invest_screener_snapshots.guards import (
        InsufficientRowsError,
        SuspiciousDistributionError,
    )

    request = snapshot_job.SnapshotBuildRequest(
        market=args.market,
        symbols=tuple(args.symbol),
        limit=args.limit,
        all_symbols=args.all,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        commit=args.commit,
        common_stocks_only=args.common_stocks_only,
    )
    use_guarded = args.commit and not args.allow_partial
    try:
        if use_guarded:
            result = await snapshot_job.run_snapshot_build_guarded(request)
        else:
            result = await snapshot_job.run_snapshot_build(request)
    except (SuspiciousDistributionError, InsufficientRowsError) as exc:
        print(f"\nCOMMIT BLOCKED: {exc}\n")
        return 2
    _print_result(result)
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="build-invest-screener-snapshots")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

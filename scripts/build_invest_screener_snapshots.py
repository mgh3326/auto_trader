#!/usr/bin/env python3
"""Build invest_screener_snapshots rows for an active KR or US universe slice.

DEFAULTS TO --dry-run: prints the SnapshotUpsert payloads it would write,
without committing to the database. Pass --commit to actually persist.

Examples:
    # KR, dry-run, top 20 active universe symbols
    uv run python -m scripts.build_invest_screener_snapshots --market kr --limit 20

    # KR, full active universe, dry-run (RECOMMENDED before any --commit)
    uv run python -m scripts.build_invest_screener_snapshots --market kr --all

    # KR, full active universe, persist (REQUIRES OPERATOR APPROVAL)
    uv run python -m scripts.build_invest_screener_snapshots --market kr --all --commit

    # US explicit symbols, persist
    uv run python -m scripts.build_invest_screener_snapshots \\
        --market us --symbol AAPL --symbol MSFT --commit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime

import sqlalchemy as sa

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.builder import build_snapshots_for_market
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)

logger = logging.getLogger(__name__)


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
        "--limit", type=int, default=None,
        help="Max active universe symbols to process. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Iterate the full active universe in --batch-size chunks. Mutually "
             "exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Symbols per processing batch when --all is set (default 200).",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write to the database. Default is --dry-run.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit is not None):
        parser.error("--all is mutually exclusive with --symbol and --limit")
    if args.limit is None:
        args.limit = 20
    args.dry_run = not args.commit
    return args


async def _resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    if override:
        return override
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse
            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
                .limit(limit)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
                .limit(limit)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def _resolve_active_universe(market: str) -> list[str]:
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse
            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(UTC).date()

    if args.all:
        symbols = await _resolve_active_universe(args.market)
        logger.info("resolved %d symbols for FULL %s universe", len(symbols), args.market)
        total_built = 0
        for batch_idx, start in enumerate(range(0, len(symbols), args.batch_size)):
            batch = symbols[start:start + args.batch_size]
            payloads = await build_snapshots_for_market(
                market=args.market, symbols=batch, today=today,
                concurrency=args.concurrency,
            )
            if not args.dry_run:
                async with AsyncSessionLocal() as session:
                    repo = InvestScreenerSnapshotsRepository(session)
                    for p in payloads:
                        await repo.upsert(p)
                    await session.commit()
            total_built += len(payloads)
            print(
                f"  batch {batch_idx + 1}: built {len(payloads)}/{len(batch)} "
                f"(running total {total_built}/{len(symbols)}) dry_run={args.dry_run}"
            )
        print(f"\nbuilt {total_built}/{len(symbols)} snapshots, "
              f"committed={'no' if args.dry_run else 'yes'}\n")
        return 0

    symbols = await _resolve_symbols(args.market, args.symbol, args.limit)
    logger.info("resolved %d symbols for market=%s", len(symbols), args.market)

    payloads = await build_snapshots_for_market(
        market=args.market, symbols=symbols, today=today, concurrency=args.concurrency
    )

    print(
        f"\nbuilt {len(payloads)}/{len(symbols)} snapshots "
        f"(market={args.market}, dry_run={args.dry_run}):"
    )
    for p in payloads[:10]:
        print(
            f"  {p.market}:{p.symbol} {p.snapshot_date} "
            f"close={p.latest_close} streak={p.consecutive_up_days} "
            f"week={p.week_change_rate}"
        )
    if len(payloads) > 10:
        print(f"  ... ({len(payloads) - 10} more)")

    if args.dry_run:
        print("\n--dry-run: no rows written.\n")
        return 0

    async with AsyncSessionLocal() as session:
        repo = InvestScreenerSnapshotsRepository(session)
        for p in payloads:
            await repo.upsert(p)
        await session.commit()
    print(f"\ncommitted {len(payloads)} rows.\n")
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="build-invest-screener-snapshots")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

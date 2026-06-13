#!/usr/bin/env python3
"""Sync Toss Open API symbol master metadata and market-cap valuation rows.

Defaults to dry-run. Pass --commit only after reviewing the printed coverage packet.
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first Toss symbol master sync (ROB-534)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to one symbol. Can be repeated.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--all", action="store_true", help="Process all active universe symbols."
    )
    parser.add_argument(
        "--no-market-cap",
        action="store_true",
        help="Update master fields only; skip prices/market cap.",
    )
    parser.add_argument(
        "--commit", action="store_true", help="Write changes. Default is dry-run."
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit != 20):
        parser.error("--all is mutually exclusive with --symbol and explicit --limit")
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    return args


def _print_result(result) -> None:
    print(
        f"\nToss symbol master {result.market.upper()} "
        f"(dry_run={not result.commit}, batches={result.batches})"
    )
    print("coverage:")
    print(f"  requested: {result.symbols_requested}")
    print(f"  stocks_matched: {result.stocks_matched}")
    print(f"  stocks_missing: {result.stocks_missing}")
    print(f"  master_updates: {result.master_updates}")
    print(f"  market_cap_payloads: {result.market_cap_payloads}")
    print(f"  market_cap_nonnull: {result.market_cap_nonnull}")
    print(
        f"  market_cap_skipped_existing: {result.market_cap_skipped_existing} "
        "(gap-fill: other source already covers the key)"
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")
    if result.samples:
        print("samples:")
        for sample in result.samples:
            print(f"  {sample}")
    if not result.commit:
        print("\n--dry-run: no rows written.\n")
    else:
        print("\ncommitted Toss symbol master updates.\n")


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.toss.client import TossReadClient
    from app.services.toss_symbol_master_service import (
        TossSymbolMasterSyncRequest,
        sync_toss_symbol_master,
    )

    client = TossReadClient.from_settings()
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await sync_toss_symbol_master(
                    session,
                    client=client,
                    request=TossSymbolMasterSyncRequest(
                        market=args.market,
                        symbols=tuple(args.symbol),
                        all_symbols=args.all,
                        limit=args.limit,
                        commit=args.commit,
                        include_market_cap=not args.no_market_cap,
                    ),
                )
            _print_result(result)
    finally:
        await client.aclose()
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="sync-toss-symbol-master")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

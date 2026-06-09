#!/usr/bin/env python3
"""Build US financial_fundamentals_snapshots from yfinance (ROB-441 PR2).

DEFAULTS TO --dry-run: fetches + parses + prints a summary WITHOUT writing. Pass
--commit only after operator approval. Annual income periods only (PR2);
quarterly/dividend are follow-ups. Production migration apply (`alembic upgrade head`)
remains operator-gated. yfinance has no per-request budget (unlike DART).
"""

from __future__ import annotations

import argparse
import asyncio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first US fundamentals (yfinance) snapshots builder (ROB-441 PR2)."
    )
    parser.add_argument("--market", choices=["us"], default="us")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to specific US tickers. Repeatable.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max active common-stock universe symbols. Defaults to 20 unless --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Iterate the full active US common-stock universe. Exclusive with --symbol/--limit.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Per-symbol fetch concurrency."
    )
    parser.add_argument(
        "--with-quarterly",
        dest="include_quarterly",
        action="store_true",
        help="Also build quarterly periods (annual-only by default; for QoQ presets).",
    )
    parser.add_argument(
        "--with-dividends",
        dest="include_dividends",
        action="store_true",
        help="Enrich annual periods with dividend_per_share + payout_ratio (for dividend presets).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually write to the database (fetches yfinance). Default is "
            "--dry-run: fetch + parse, no writes."
        ),
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
        f"\nbuilt {result.snapshots_built} US fundamentals snapshots "
        f"for {result.symbols_resolved} symbols (dry_run={not result.committed}):"
    )
    if result.samples:
        print("samples:")
        for sample in result.samples:
            print(f"  {sample}")
    if result.warnings:
        print(f"warnings ({len(result.warnings)}):")
        for warning in result.warnings[:20]:
            print(f"  - {warning}")
        if len(result.warnings) > 20:
            print(f"  ... (+{len(result.warnings) - 20} more)")
    if not result.committed:
        print("\n--dry-run: no rows written.\n")
    else:
        print(f"\ncommitted {result.snapshots_built} rows.\n")


async def run(args: argparse.Namespace) -> int:
    from app.services.financial_fundamentals_snapshots.builder_us import (
        build_us_fundamentals_for_symbols,
        resolve_us_symbols,
    )

    symbols = await resolve_us_symbols(
        override=list(args.symbol), limit=args.limit, all_symbols=args.all
    )
    if not symbols:
        print("No US symbols resolved (build/refresh us_symbol_universe first).")
        return 0
    print(
        f"Resolved {len(symbols)} US symbols. NOTE: --dry-run still fetches yfinance."
    )
    result = await build_us_fundamentals_for_symbols(
        symbols,
        commit=args.commit,
        concurrency=args.concurrency,
        include_quarterly=args.include_quarterly,
        include_dividends=args.include_dividends,
    )
    _print_result(result)
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="build-us-fundamentals-snapshots")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Bounded, read-only preview of the support_proximity screener preset (ROB-976).

DEFAULTS TO --dry-run (the ONLY mode): support_proximity has no persisted
artifact of its own. The underlying invest_screener_snapshots partition
(built by scripts/build_invest_screener_snapshots.py, --market kr --all) is
the "야간 스냅샷 배치" this preset depends on for candidate price/quality
inputs; DB writes stay scoped to InvestScreenerSnapshotsRepository.upsert
(that batch's writer), which this script never calls. This script runs the
SAME bounded two-stage pipeline the screen_stocks_snapshot MCP tool uses
(snapshot-only Bollinger proxy -> bounded live get_support_resistance
re-verification of the top candidates) and prints the ranked result, so an
operator can smoke-test the preset (or run it nightly to warm the OHLCV
cache) without going through MCP.

Examples:
    # KR, default quality floors + limit
    uv run python -m scripts.build_support_proximity_snapshot --market kr

    # Tighter/looser floors, smaller candidate pool
    uv run python -m scripts.build_support_proximity_snapshot \
        --market kr --limit 10 --min-market-cap 500000000000 \
        --candidate-pool-limit 15
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only bounded preview of the support_proximity screener "
            "preset (ROB-976). No --commit — see module docstring."
        )
    )
    parser.add_argument("--market", choices=["kr"], default="kr")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--min-market-cap", type=float, default=None)
    parser.add_argument("--min-turnover", type=float, default=None)
    parser.add_argument("--candidate-pool-limit", type=int, default=None)
    return parser.parse_args(argv)


def _print_result(result) -> None:
    print(
        f"\nsupport_proximity preview (market=kr, partition={result.partition_date}, "
        f"degradation_reason={result.degradation_reason}, "
        f"coverage_label={result.coverage_label}):"
    )
    if not result.rows:
        print("  (no rows — see degradation_reason above)")
    for row in result.rows:
        print(
            f"  {row['symbol']} {row.get('name') or '-'}: "
            f"close={row['close']:.0f} support={row.get('support_price')} "
            f"({row.get('support_kind')}, {row.get('support_strength')}) "
            f"dist={row['dist_to_support_pct']:.2f}% "
            f"market_cap={row.get('market_cap')}"
        )
    print(
        f"\n{len(result.rows)} row(s) — dry-run only, no rows written "
        "(no persisted artifact for this preset; see module docstring).\n"
    )


async def run(args: argparse.Namespace) -> int:
    from app.services.invest_view_model.support_proximity_screener import (
        DEFAULT_CANDIDATE_POOL_LIMIT,
        load_support_proximity_from_snapshots,
    )

    async with AsyncSessionLocal() as session:
        result = await load_support_proximity_from_snapshots(
            session,
            market=args.market,
            limit=args.limit,
            min_market_cap=args.min_market_cap,
            min_turnover=args.min_turnover,
            candidate_pool_limit=args.candidate_pool_limit
            or DEFAULT_CANDIDATE_POOL_LIMIT,
        )
    if result is None:
        print(
            "\nno invest_screener_snapshots partition found for market=kr — run "
            "scripts/build_invest_screener_snapshots.py --market kr --all --commit "
            "first.\n"
        )
        return 1
    _print_result(result)
    return 0


async def main() -> int:
    setup_logging_and_sentry(service_name="build-support-proximity-snapshot")
    return await run(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

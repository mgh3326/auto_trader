#!/usr/bin/env python3
"""Read-only invest_screener_snapshots coverage diagnostic CLI (ROB-170).

Prints a coverage summary for the invest_screener_snapshots table.
NEVER writes to the database; safe to run against production.

Examples:
    uv run python -m scripts.diagnose_invest_screener_snapshots --market kr
    uv run python -m scripts.diagnose_invest_screener_snapshots --market us
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.coverage_service import build_coverage

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only invest_screener_snapshots coverage CLI (ROB-170)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="diagnose-invest-screener-snapshots")
    args = parse_args(argv)
    try:
        async with AsyncSessionLocal() as session:
            report = await build_coverage(session, market=args.market)
        print(
            f"\nmarket={report.market} asOf={report.asOf.isoformat()}\n"
            f"  universe={report.totalSymbolsInUniverse}\n"
            f"  coveringToday={report.snapshotsCoveringToday}\n"
            f"  stale={report.snapshotsStale}\n"
            f"  missing={report.snapshotsMissing}\n"
            f"  lastComputedAt={report.lastComputedAt}\n"
            f"  dataState={report.dataState}\n"
        )
        return 0
    except Exception as exc:
        logger.exception("diagnose_invest_screener_snapshots crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

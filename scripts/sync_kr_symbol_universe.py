#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.jobs.kr_symbol_universe import run_kr_symbol_universe_sync

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync the KR symbol universe.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and diff the source snapshot without writing database rows.",
    )
    return parser.parse_args(argv)


async def main(*, dry_run: bool = False) -> int:
    setup_logging_and_sentry(service_name="kr-symbol-universe-sync")

    async def _job() -> int:
        result = await run_kr_symbol_universe_sync(dry_run=dry_run)
        if result.get("status") != "completed":
            logger.error("KR symbol universe sync failed: %s", result)
            return 1
        logger.info(
            "KR symbol universe %s completed: %s",
            "dry-run" if dry_run else "sync",
            result,
        )
        return 0

    return await run_async_job(_job, process="sync_kr_symbol_universe")


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(main(dry_run=args.dry_run)))

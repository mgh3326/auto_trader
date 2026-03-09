#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import cast

from app.core.config import settings
from app.jobs.us_candles import run_us_candles_sync
from app.monitoring.sentry import capture_exception, init_sentry

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync US candles (1m/1h pipeline source)"
    )
    _ = parser.add_argument(
        "--mode",
        choices=["incremental", "backfill"],
        default="incremental",
        help="Sync mode (default: incremental)",
    )
    _ = parser.add_argument(
        "--sessions",
        type=int,
        default=10,
        help="Backfill trading sessions (default: 10)",
    )
    _ = parser.add_argument(
        "--user-id",
        type=int,
        default=1,
        help="Manual holdings user id (default: 1)",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    mode = cast(str, args.mode)
    sessions = cast(int, args.sessions)
    user_id = cast(int, args.user_id)

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    _ = init_sentry(service_name="us-candles-sync")

    try:
        result = await run_us_candles_sync(
            mode=mode,
            sessions=max(sessions, 1),
            user_id=user_id,
        )
    except Exception as exc:
        capture_exception(exc, process="sync_us_candles")
        logger.error("US candles sync crashed: %s", exc, exc_info=True)
        return 1

    if result.get("status") != "completed":
        logger.error("US candles sync failed: %s", result)
        return 1

    logger.info("US candles sync completed: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

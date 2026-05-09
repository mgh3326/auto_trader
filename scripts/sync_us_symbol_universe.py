#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import logging

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.jobs.us_symbol_universe import run_us_symbol_universe_sync

logger = logging.getLogger(__name__)


async def main() -> int:
    setup_logging_and_sentry(service_name="us-symbol-universe-sync")

    async def _job() -> int:
        result = await run_us_symbol_universe_sync()
        if result.get("status") != "completed":
            logger.error("US symbol universe sync failed: %s", result)
            return 1
        logger.info("US symbol universe sync completed: %s", result)
        return 0

    return await run_async_job(_job, process="sync_us_symbol_universe")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

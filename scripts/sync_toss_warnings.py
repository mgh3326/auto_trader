#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import logging

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.jobs.toss_warnings import run_toss_warnings_sync

logger = logging.getLogger(__name__)


async def main() -> int:
    setup_logging_and_sentry(service_name="toss-warnings-sync")

    async def _job() -> int:
        result = await run_toss_warnings_sync()
        if result.get("status") != "completed":
            logger.error("Toss warnings sync failed: %s", result)
            return 1
        logger.info("Toss warnings sync completed: %s", result)
        return 0

    return await run_async_job(_job, process="sync_toss_warnings")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

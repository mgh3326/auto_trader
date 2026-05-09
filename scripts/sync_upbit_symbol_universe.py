#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import logging

from app.core.cli import run_async_job, setup_logging_and_sentry
from app.jobs.upbit_symbol_universe import run_upbit_symbol_universe_sync

logger = logging.getLogger(__name__)


async def main() -> int:
    setup_logging_and_sentry(service_name="upbit-symbol-universe-sync")

    async def _job() -> int:
        result = await run_upbit_symbol_universe_sync()
        if result.get("status") != "completed":
            logger.error("Upbit symbol universe sync failed: %s", result)
            return 1
        logger.info("Upbit symbol universe sync completed: %s", result)
        return 0

    return await run_async_job(_job, process="sync_upbit_symbol_universe")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

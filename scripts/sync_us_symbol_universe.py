#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.jobs.us_symbol_universe import run_us_symbol_universe_sync
from app.monitoring.sentry import capture_exception, init_sentry

logger = logging.getLogger(__name__)


async def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="us-symbol-universe-sync")

    try:
        result = await run_us_symbol_universe_sync()
    except Exception as exc:
        capture_exception(exc, process="sync_us_symbol_universe")
        logger.error("US symbol universe sync crashed: %s", exc, exc_info=True)
        return 1

    if result.get("status") != "completed":
        logger.error("US symbol universe sync failed: %s", result)
        return 1

    logger.info("US symbol universe sync completed: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

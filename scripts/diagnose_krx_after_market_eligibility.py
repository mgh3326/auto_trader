#!/usr/bin/env python3
"""SELECT-only KRX eligibility freshness check; exit 1 means operator attention."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import text

from app.core.cli import setup_logging_and_sentry
from app.core.db import engine
from app.services.krx_after_market_health import load_krx_after_market_health

logger = logging.getLogger(__name__)


async def run() -> int:
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(postgresql_readonly=True)
            async with conn.begin():
                await conn.execute(
                    text("SELECT set_config('statement_timeout', '8000', true)")
                )
                health = await load_krx_after_market_health(conn, now=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 — diagnostic failure must be visible
        health = {
            "status": "unavailable",
            "requires_attention": True,
            "error_class": type(exc).__name__,
        }
    finally:
        try:
            await engine.dispose()
        except Exception as exc:  # noqa: BLE001 — cleanup errors are sanitized too
            health = {
                "status": "unavailable",
                "requires_attention": True,
                "error_class": type(exc).__name__,
            }
    print(json.dumps(health, sort_keys=True))
    if health["requires_attention"]:
        logger.error(
            "KRX after-market eligibility requires attention: %s", health["status"]
        )
        return 1
    return 0


if __name__ == "__main__":
    setup_logging_and_sentry(service_name="krx-after-market-eligibility-health")
    raise SystemExit(asyncio.run(run()))

#!/usr/bin/env python3
"""Manual/Prefect entrypoint for ROB-99 crypto pending-order reminders."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.core.config import settings
from app.jobs.crypto_pending_order_alert import run_crypto_pending_order_alert

try:  # Prefect is a runtime dependency, but keep import errors explicit for operators.
    from prefect import flow, get_run_logger, task
except ImportError as exc:  # pragma: no cover - exercised only in broken envs
    raise SystemExit(
        "Prefect is required for this entrypoint. Install dependencies with `uv sync`."
    ) from exc


@task(name="crypto-pending-order-alert-cycle")
async def crypto_pending_order_alert_task(*, execute: bool) -> dict[str, Any]:
    logger = get_run_logger()
    result = await run_crypto_pending_order_alert(execute=execute)
    logger.info("crypto_pending_order_alert result: %s", result)
    return result


@flow(name="crypto-pending-order-alert")
async def crypto_pending_order_alert_flow(*, execute: bool = True) -> dict[str, Any]:
    """Read-only Prefect flow for Upbit KRW pending-order reminders."""

    return await crypto_pending_order_alert_task(execute=execute)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or serve the read-only Upbit KRW pending-order reminder."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch/format only. Never send Discord messages.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Send Discord messages when enabled and required.",
    )
    mode.add_argument(
        "--serve",
        action="store_true",
        help="Serve the Prefect flow on configured daily KST schedules.",
    )
    return parser.parse_args()


def _schedule_crons() -> list[str]:
    schedules = [
        item.strip()
        for item in settings.CRYPTO_PENDING_ORDER_SCHEDULES.split(",")
        if item.strip()
    ]
    crons: list[str] = []
    for item in schedules:
        hour_str, minute_str = item.split(":", maxsplit=1)
        crons.append(f"{int(minute_str)} {int(hour_str)} * * *")
    return crons


def main() -> None:
    args = _parse_args()
    if args.serve:
        # Prefect serves one process with multiple daily cron schedules.  The flow
        # still respects CRYPTO_PENDING_ORDER_ALERT_ENABLED at execution time.
        crypto_pending_order_alert_flow.serve(
            name="crypto-pending-order-alert",
            cron=_schedule_crons(),
            timezone=settings.CRYPTO_PENDING_ORDER_TIMEZONE,
            parameters={"execute": True},
        )
        return

    result = asyncio.run(crypto_pending_order_alert_flow(execute=args.execute))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

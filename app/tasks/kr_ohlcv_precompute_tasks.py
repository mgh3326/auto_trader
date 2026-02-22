from __future__ import annotations

import logging

from app.core.taskiq_broker import broker
from app.jobs.kr_ohlcv_precompute import (
    run_kr_ohlcv_incremental_precompute,
    run_kr_ohlcv_nightly_precompute,
)

logger = logging.getLogger(__name__)


@broker.task(
    task_name="ohlcv.kr.precompute.incremental",
    schedule=[{"cron": "*/5 8-20 * * 1-5", "cron_offset": "Asia/Seoul"}],
)
async def run_kr_ohlcv_incremental_precompute_task() -> dict[str, int | str]:
    try:
        return await run_kr_ohlcv_incremental_precompute()
    except Exception as exc:
        logger.error(
            "TaskIQ KR OHLCV incremental precompute failed: %s",
            exc,
            exc_info=True,
        )
        return {
            "status": "failed",
            "mode": "incremental",
            "error": str(exc),
        }


@broker.task(
    task_name="ohlcv.kr.precompute.nightly",
    schedule=[{"cron": "25 2 * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_kr_ohlcv_nightly_precompute_task() -> dict[str, int | str]:
    try:
        return await run_kr_ohlcv_nightly_precompute()
    except Exception as exc:
        logger.error(
            "TaskIQ KR OHLCV nightly precompute failed: %s",
            exc,
            exc_info=True,
        )
        return {
            "status": "failed",
            "mode": "nightly",
            "error": str(exc),
        }

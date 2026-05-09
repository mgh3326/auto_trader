"""Shared CLI bootstrap helpers for auto_trader scripts.

Provides:
- setup_logging_and_sentry: logging.basicConfig + init_sentry in one call
- run_async_job: common try/except wrapper for sync-style async jobs
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.core.config import settings
from app.monitoring.sentry import capture_exception, init_sentry

__all__ = ["run_async_job", "setup_logging_and_sentry"]

_logger = logging.getLogger(__name__)


def setup_logging_and_sentry(service_name: str) -> None:
    """Configure root logger and initialize Sentry for a CLI service.

    Always applies .upper() to settings.LOG_LEVEL; unknown levels fall back to INFO.
    """
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name=service_name)


async def run_async_job(
    coro_factory: Callable[[], Awaitable[int]],
    *,
    process: str,
) -> int:
    """Run an async job coroutine, capturing exceptions to Sentry.

    Returns the integer exit code from coro_factory, or 1 on exception.
    SystemExit and KeyboardInterrupt are NOT caught.

    Usage:
        return await run_async_job(_job, process="sync_kr_candles")
    """
    try:
        return await coro_factory()
    except Exception as exc:
        capture_exception(exc, process=process)
        _logger.error("%s crashed: %s", process, exc, exc_info=True)
        return 1

"""Process-shared cache for the composed portfolio read model.

The Toss-specific cache remains a separate namespace because its payload and
invalidation contract are narrower.  This facade uses the same Redis-backed
singleflight implementation with a portfolio-wide key namespace.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any

import redis.asyncio as redis

from app.core.config import settings
from app.services.portfolio_snapshot import portfolio_snapshot_scope
from app.services.toss_portfolio_snapshot_cache import TossPortfolioSnapshotCache

logger = logging.getLogger(__name__)

_KEY_PREFIX = "portfolio:snapshot:v1"
_LOCK_PREFIX = "portfolio:snapshot:singleflight:v1"


class PortfolioSnapshotCache(TossPortfolioSnapshotCache):
    """Redis cache facade for the complete composed portfolio read model."""

    def __init__(
        self,
        *,
        redis_client: Any,
        ttl_seconds: float,
        lock_ttl_seconds: float = 10.0,
        wait_timeout_seconds: float = 3.0,
        poll_interval_seconds: float = 0.05,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            ttl_seconds=ttl_seconds,
            lock_ttl_seconds=lock_ttl_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            enabled=enabled,
            key_prefix=_KEY_PREFIX,
            lock_prefix=_LOCK_PREFIX,
        )


_shared_portfolio_snapshot_cache: PortfolioSnapshotCache | None = None
_shared_snapshot_redis_client: Any | None = None


def _close_owned_redis_client(client: Any) -> None:
    """Best-effort release of a Redis client this module created.

    The reset hook is synchronous but ``redis.asyncio`` closes are awaitable,
    so schedule the close on the running loop when there is one and drain it
    directly otherwise. Every failure path is suppressed: dropping the
    singleton must never raise, but it must also not leak the connection pool.
    """

    if client is None:
        return
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        result = closer()
    except Exception as exc:  # noqa: BLE001 — teardown never raises
        logger.warning("Snapshot cache Redis close failed: %s", exc)
        return
    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with contextlib.suppress(Exception):
            asyncio.run(_drain(result))
        return
    task = loop.create_task(_drain(result))
    task.add_done_callback(lambda finished: finished.exception())


async def _drain(awaitable: Any) -> None:
    with contextlib.suppress(Exception):
        await awaitable


def get_shared_portfolio_snapshot_cache() -> PortfolioSnapshotCache:
    """Return a process-local Redis facade over the shared portfolio store."""

    global _shared_portfolio_snapshot_cache
    if _shared_portfolio_snapshot_cache is None:
        redis_client = None
        try:
            redis_client = redis.from_url(
                settings.get_redis_url(),
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001 — cache fails open
            logger.warning("Portfolio snapshot Redis client init failed: %s", exc)

        # The test suite must not accidentally share state through a developer
        # Redis.  Production/development retain the operator-controlled gate;
        # tests use explicit fakeredis facades for cross-process assertions.
        enabled = bool(
            getattr(settings, "portfolio_snapshot_cache_enabled", True)
        ) and str(getattr(settings, "ENVIRONMENT", "development")).lower() not in {
            "test",
            "testing",
        }
        global _shared_snapshot_redis_client
        _shared_snapshot_redis_client = redis_client
        _shared_portfolio_snapshot_cache = PortfolioSnapshotCache(
            redis_client=redis_client,
            ttl_seconds=float(
                getattr(settings, "portfolio_snapshot_cache_ttl_seconds", 5.0)
            ),
            lock_ttl_seconds=float(
                getattr(settings, "portfolio_snapshot_cache_lock_ttl_seconds", 10.0)
            ),
            wait_timeout_seconds=float(
                getattr(settings, "portfolio_snapshot_cache_wait_seconds", 3.0)
            ),
            enabled=enabled,
        )
    return _shared_portfolio_snapshot_cache


def reset_shared_portfolio_snapshot_cache() -> None:
    """Test hook for dropping the process-local Redis facade."""

    global _shared_portfolio_snapshot_cache, _shared_snapshot_redis_client
    _shared_portfolio_snapshot_cache = None
    client = _shared_snapshot_redis_client
    _shared_snapshot_redis_client = None
    _close_owned_redis_client(client)


__all__ = [
    "PortfolioSnapshotCache",
    "get_shared_portfolio_snapshot_cache",
    "portfolio_snapshot_scope",
    "reset_shared_portfolio_snapshot_cache",
]

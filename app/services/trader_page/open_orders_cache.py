"""TTL snapshot for the /trader open-orders panel + cross-process fill bust.

The payload itself is cached in-process (the API runs as one process). The
invalidation generation lives in Redis because fills normally commit inside
the websocket monitor process (``WS_LEDGER_SINK=db``): the post-upsert hook
INCRs the key there, and the API notices on its next read. Every Redis call
fails open — worst case is bounded by the configured TTL, never by Redis.

This module is imported by ``app.services.execution_ledger.fill_ingest`` for
the post-commit hook, so it must stay free of broker-client and DB imports.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import redis.asyncio as redis

from app.core.config import settings

if TYPE_CHECKING:
    from app.schemas.open_orders import OpenOrdersResponse

logger = logging.getLogger(__name__)

GEN_KEY = "trader_page:open_orders:gen"


async def _default_redis_factory() -> redis.Redis:
    return redis.from_url(
        settings.get_redis_url(),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        decode_responses=True,
    )


class TraderOpenOrdersCache:
    """In-process snapshot guarded by TTL and a shared Redis generation key.

    ``record_last_ok`` bookkeeping lives outside the snapshot on purpose: a
    dropped payload must not erase the per-source "last successful read" times
    the panel shows while a broker is failing.
    """

    def __init__(
        self,
        *,
        redis_factory: Callable[[], Awaitable[redis.Redis]] | None = None,
    ) -> None:
        self._snapshot: OpenOrdersResponse | None = None
        self._expires_at: datetime | None = None
        self._cached_at: datetime | None = None
        self._seen_gen: int | None = None
        self._last_ok: dict[tuple[str, str], datetime] = {}
        self._redis_factory = redis_factory or _default_redis_factory
        self._redis: redis.Redis | None = None

    async def read(
        self, now: datetime
    ) -> tuple[OpenOrdersResponse | None, datetime | None]:
        """Return (snapshot, cached_at) on a hit, else (None, None)."""
        if (
            self._snapshot is None
            or self._expires_at is None
            or now >= self._expires_at
        ):
            return None, None
        gen = await self.generation()
        if gen is not None and gen != self._seen_gen:
            self.invalidate_local()
            return None, None
        return self._snapshot, self._cached_at

    async def store(
        self,
        payload: OpenOrdersResponse,
        now: datetime,
        ttl_seconds: int,
        *,
        seen_gen: int | None,
    ) -> None:
        """Pin the snapshot to the generation captured BEFORE the broker fetch.

        Callers must pass the generation they read before fetching orders:
        a fill committed mid-fetch then shows up as a generation mismatch on
        the next read instead of being absorbed into a stale snapshot.
        """
        self._snapshot = payload
        self._cached_at = now
        self._expires_at = now + timedelta(seconds=ttl_seconds)
        self._seen_gen = seen_gen

    def record_last_ok(self, response: OpenOrdersResponse) -> None:
        for source in response.sources:
            if source.status == "ok" and source.fetched_at is not None:
                self._last_ok[(source.broker, source.market)] = source.fetched_at

    def last_ok(self, broker: str, market: str) -> datetime | None:
        return self._last_ok.get((broker, market))

    def invalidate_local(self) -> None:
        self._snapshot = None
        self._expires_at = None
        self._cached_at = None

    async def _redis_client(self) -> redis.Redis | None:
        if self._redis is None:
            try:
                self._redis = await self._redis_factory()
            except Exception:  # noqa: BLE001 - cache must fail open
                logger.debug("trader_page cache: redis init failed", exc_info=True)
                return None
        return self._redis

    async def generation(self) -> int | None:
        client = await self._redis_client()
        if client is None:
            return None
        try:
            raw = await client.get(GEN_KEY)
        except Exception:  # noqa: BLE001 - fail open to TTL-only freshness
            logger.debug("trader_page cache: gen read failed", exc_info=True)
            return None
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def bump_generation(self) -> None:
        client = await self._redis_client()
        if client is None:
            return
        try:
            await client.incr(GEN_KEY)
        except Exception:  # noqa: BLE001 - invalidation is best-effort
            logger.debug("trader_page cache: gen bump failed", exc_info=True)


OPEN_ORDERS_CACHE = TraderOpenOrdersCache()


async def invalidate_open_orders_cache() -> None:
    """Post-commit fill hook: drop this process's snapshot and bump the shared
    generation so every other API replica drops its copy too. Best-effort — a
    failure here must never reach the fill commit path."""
    OPEN_ORDERS_CACHE.invalidate_local()
    await OPEN_ORDERS_CACHE.bump_generation()

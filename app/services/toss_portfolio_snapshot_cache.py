"""Process-shared Redis cache and singleflight for Toss portfolio snapshots.

The cache contains only the holdings/buying-power read model.  It deliberately
does not provide a sellable quantity: order paths must query the broker at the
time of the order and must never use this read cache for sizing or approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis
from redis.exceptions import WatchError

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "toss:portfolio:snapshot:v1"
_LOCK_PREFIX = "toss:portfolio:snapshot:singleflight:v1"


def _key(scope: str) -> str:
    return f"{_KEY_PREFIX}:{scope}"


def _lock_key(scope: str) -> str:
    return f"{_LOCK_PREFIX}:{scope}"


class TossPortfolioSnapshotCache:
    """Redis-backed cache-aside store with a bounded distributed singleflight."""

    def __init__(
        self,
        *,
        redis_client: Any,
        ttl_seconds: float,
        lock_ttl_seconds: float = 10.0,
        wait_timeout_seconds: float = 3.0,
        poll_interval_seconds: float = 0.05,
        enabled: bool = True,
        key_prefix: str = _KEY_PREFIX,
        lock_prefix: str = _LOCK_PREFIX,
    ) -> None:
        self._redis = redis_client
        self._ttl_seconds = float(ttl_seconds)
        self._lock_ttl_seconds = float(lock_ttl_seconds)
        self._wait_timeout_seconds = max(float(wait_timeout_seconds), 0.0)
        self._poll_interval_seconds = max(float(poll_interval_seconds), 0.01)
        self._enabled = bool(enabled)
        self._key_prefix = key_prefix
        self._lock_prefix = lock_prefix
        self._degraded_until = 0.0

    def _mark_degraded(self) -> None:
        self._degraded_until = time.monotonic() + 5.0

    @property
    def usable(self) -> bool:
        return (
            self._enabled
            and self._ttl_seconds > 0
            and self._redis is not None
            and time.monotonic() >= self._degraded_until
        )

    def _cache_key(self, scope: str) -> str:
        return f"{self._key_prefix}:{scope}"

    def _singleflight_key(self, scope: str) -> str:
        return f"{self._lock_prefix}:{scope}"

    async def get(self, scope: str) -> dict[str, Any] | None:
        if not self.usable:
            return None
        try:
            raw = await self._redis.get(self._cache_key(scope))
        except Exception as exc:  # noqa: BLE001 — read cache fails open
            self._mark_degraded()
            logger.warning("Toss portfolio snapshot cache GET failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Toss portfolio snapshot cache entry is invalid")
            return None
        return payload if isinstance(payload, dict) else None

    async def put(self, scope: str, payload: dict[str, Any]) -> None:
        if not self.usable:
            return
        try:
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            await self._redis.set(
                self._cache_key(scope),
                encoded,
                ex=max(1, int(self._ttl_seconds)),
            )
        except Exception as exc:  # noqa: BLE001 — write cache fails open
            self._mark_degraded()
            logger.warning("Toss portfolio snapshot cache SET failed: %s", exc)

    async def delete(self, scope: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._cache_key(scope))
        except Exception as exc:  # noqa: BLE001 — invalidation is best effort
            logger.warning("Toss portfolio snapshot cache DEL failed: %s", exc)

    async def _acquire(self, scope: str) -> str | None:
        if not self.usable:
            return None
        token = str(uuid.uuid4())
        try:
            acquired = await self._redis.set(
                self._singleflight_key(scope),
                token,
                nx=True,
                px=max(1, int(self._lock_ttl_seconds * 1000)),
            )
        except Exception as exc:  # noqa: BLE001 — singleflight fails open
            self._mark_degraded()
            logger.warning("Toss portfolio snapshot lock failed: %s", exc)
            return None
        return token if acquired else None

    async def _release(self, scope: str, token: str) -> None:
        if self._redis is None:
            return
        lock_key = self._singleflight_key(scope)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(lock_key)
                if await pipe.get(lock_key) != token:
                    return
                pipe.multi()
                pipe.delete(lock_key)
                await pipe.execute()
        except WatchError:
            # Another owner acquired the key after this lease expired.
            return
        except Exception:  # noqa: BLE001 — lock expiry is the fallback
            return

    async def _renew(self, scope: str, token: str) -> bool:
        if not self.usable or self._redis is None:
            return False
        lock_key = self._singleflight_key(scope)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(lock_key)
                if await pipe.get(lock_key) != token:
                    return False
                pipe.multi()
                pipe.pexpire(
                    lock_key,
                    max(1, int(self._lock_ttl_seconds * 1000)),
                )
                renewed = await pipe.execute()
        except WatchError:
            # The lease expired or changed ownership while being renewed.
            return False
        except Exception as exc:  # noqa: BLE001 — renewal failure is fail-open
            self._mark_degraded()
            logger.warning("Toss portfolio snapshot lock renewal failed: %s", exc)
            return False
        return bool(renewed and renewed[0])

    async def _renew_until_done(self, scope: str, token: str) -> None:
        interval = max(min(self._lock_ttl_seconds / 3.0, 1.0), 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await self._renew(scope, token):
                return

    async def _lock_remaining_seconds(self, scope: str) -> float | None:
        """Return the current lock lease, or ``None`` when Redis is unavailable.

        A waiter may extend its initial bounded polling window while the owner
        still holds a live lease.  The lease is the distributed ownership
        boundary; after it expires, a direct fetch is the bounded crash
        recovery path.
        """

        if not self.usable or self._redis is None:
            return None
        try:
            pttl = await self._redis.pttl(self._singleflight_key(scope))
        except Exception as exc:  # noqa: BLE001 — lock inspection fails open
            self._mark_degraded()
            logger.warning("Toss portfolio snapshot lock TTL failed: %s", exc)
            return None

        if pttl == -2:
            return 0.0
        if pttl == -1:
            # Our locks always have a lease.  A pre-existing/malformed
            # immortal lock is not demonstrably live, so fail open rather
            # than waiting without a bound.
            return 0.0
        if pttl < 0:
            return 0.0
        return max(float(pttl) / 1000.0, 0.0)

    async def get_or_fetch(
        self,
        scope: str,
        fetcher: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Return a cached payload, sharing one upstream fetch across processes.

        Redis outages and lock wait exhaustion fall back to the supplied fetcher;
        no synthetic payload is created when the cache is unavailable.
        """
        cached = await self.get(scope)
        if cached is not None:
            return cached
        if not self.usable:
            return await fetcher()

        token = await self._acquire(scope)
        if token is not None:
            renewal_task = asyncio.create_task(self._renew_until_done(scope, token))
            try:
                # Another writer may have completed between the initial GET and
                # lock acquisition.
                cached = await self.get(scope)
                if cached is not None:
                    return cached
                payload = await fetcher()
                if isinstance(payload, dict):
                    await self.put(scope, payload)
                return payload
            finally:
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
                await self._release(scope, token)

        if not self.usable:
            return await fetcher()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                lock_remaining = await self._lock_remaining_seconds(scope)
                if lock_remaining is None or lock_remaining <= 0:
                    break
                # A positive PTTL is proof that some owner still has a live
                # lease.  Follow that lease rather than using a fixed total
                # deadline: a renewal moves the next observation forward,
                # while an owner death or failed renewal lets the lease expire
                # and bounds the direct-fetch escape.
                deadline = loop.time() + lock_remaining
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
            await asyncio.sleep(min(self._poll_interval_seconds, remaining))
            cached = await self.get(scope)
            if cached is not None:
                return cached

        # The owner may have failed or Redis may be unhealthy.  Returning a
        # direct upstream result is safer than inventing a stale sellable value.
        return await fetcher()


_shared_portfolio_snapshot_cache: TossPortfolioSnapshotCache | None = None


def get_shared_portfolio_snapshot_cache() -> TossPortfolioSnapshotCache:
    """Return a process-local Redis client facade over the shared snapshot store."""
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
            logger.warning("Toss portfolio snapshot Redis client init failed: %s", exc)
        _shared_portfolio_snapshot_cache = TossPortfolioSnapshotCache(
            redis_client=redis_client,
            ttl_seconds=float(
                getattr(settings, "toss_portfolio_snapshot_cache_ttl_seconds", 5.0)
            ),
            lock_ttl_seconds=float(
                getattr(
                    settings,
                    "toss_portfolio_snapshot_cache_lock_ttl_seconds",
                    10.0,
                )
            ),
            wait_timeout_seconds=float(
                getattr(
                    settings,
                    "toss_portfolio_snapshot_cache_wait_seconds",
                    3.0,
                )
            ),
            enabled=bool(
                getattr(settings, "toss_portfolio_snapshot_cache_enabled", True)
            ),
        )
    return _shared_portfolio_snapshot_cache


def reset_shared_portfolio_snapshot_cache() -> None:
    """Test hook for dropping the process-local Redis facade."""
    global _shared_portfolio_snapshot_cache
    _shared_portfolio_snapshot_cache = None


__all__ = [
    "TossPortfolioSnapshotCache",
    "get_shared_portfolio_snapshot_cache",
    "reset_shared_portfolio_snapshot_cache",
]

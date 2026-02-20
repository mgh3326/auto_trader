from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

TaskResult = dict[str, Any]


@dataclass(slots=True)
class TaskLock:
    redis_client: redis.Redis
    lock_key: str
    token: str
    _released: bool = False

    async def release(self) -> bool:
        if self._released:
            return False

        released = False
        try:
            result = await self.redis_client.eval(
                _RELEASE_LOCK_SCRIPT,
                1,
                self.lock_key,
                self.token,
            )
            released = bool(result)
        except Exception:
            logger.exception("Failed to release task lock: %s", self.lock_key)
        finally:
            self._released = True
            await self.redis_client.aclose()

        return released


async def acquire_task_lock(
    lock_key: str,
    ttl_seconds: int,
    redis_url: str | None = None,
) -> TaskLock | None:
    resolved_redis_url = redis_url or settings.get_redis_url()
    redis_client = redis.from_url(
        resolved_redis_url,
        decode_responses=True,
    )
    token = uuid.uuid4().hex

    try:
        acquired = await redis_client.set(
            lock_key,
            token,
            nx=True,
            ex=max(int(ttl_seconds), 1),
        )
        if not acquired:
            await redis_client.aclose()
            return None
        return TaskLock(redis_client=redis_client, lock_key=lock_key, token=token)
    except Exception:
        await redis_client.aclose()
        logger.exception("Failed to acquire task lock: %s", lock_key)
        raise


async def run_with_task_lock(
    lock_key: str,
    ttl_seconds: int,
    coro_factory: Callable[[], Awaitable[TaskResult]],
    redis_url: str | None = None,
) -> TaskResult:
    lock = await acquire_task_lock(
        lock_key=lock_key,
        ttl_seconds=ttl_seconds,
        redis_url=redis_url,
    )
    if lock is None:
        return {
            "status": "skipped",
            "reason": "lock_held",
            "lock_key": lock_key,
        }

    try:
        return await coro_factory()
    finally:
        await lock.release()

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
import random
import time
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class TossApiGroup(StrEnum):
    AUTH = "AUTH"
    ACCOUNT = "ACCOUNT"
    ASSET = "ASSET"
    STOCK = "STOCK"
    MARKET_INFO = "MARKET_INFO"
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"
    ORDER = "ORDER"
    ORDER_HISTORY = "ORDER_HISTORY"
    ORDER_INFO = "ORDER_INFO"


_PEAK_WINDOW_GROUPS = frozenset({TossApiGroup.ORDER, TossApiGroup.ORDER_INFO})
_REDIS_WINDOW_MILLISECONDS: Final[int] = 1_000
_REDIS_FALLBACK_WARNING_INTERVAL_SECONDS: Final[float] = 60.0


def _client_fingerprint(client_id: str) -> str:
    """Match Toss OAuth's non-secret client-id namespace convention."""

    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def toss_rate_limit_key(client_id: str, group: TossApiGroup) -> str:
    """Return the client-scoped Redis key without exposing the client id."""

    return f"toss:ratelimit:{_client_fingerprint(client_id)}:{group.value}"


_BASE_LIMITS: dict[TossApiGroup, int] = {
    TossApiGroup.AUTH: 5,
    TossApiGroup.ACCOUNT: 1,
    TossApiGroup.ASSET: 5,
    TossApiGroup.STOCK: 5,
    TossApiGroup.MARKET_INFO: 3,
    TossApiGroup.MARKET_DATA: 10,
    TossApiGroup.MARKET_DATA_CHART: 5,
    TossApiGroup.ORDER: 6,
    TossApiGroup.ORDER_HISTORY: 5,
    TossApiGroup.ORDER_INFO: 6,
}


# The Redis server owns the sliding-window clock.  ``application_limit`` is
# recalculated on every Python retry via ``limit_for``; the Lua guard also
# derives the KST 09:00--09:10 peak cap from Redis TIME.  Taking the stricter
# of both values prevents an in-flight 6->3 boundary transition from admitting
# a fourth request because a worker's wall clock was briefly behind Redis.
_REDIS_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local application_limit = tonumber(ARGV[1])
local base_limit = tonumber(ARGV[2])
local peak_window_group = tonumber(ARGV[3])
local member_suffix = ARGV[4]
local window_ms = tonumber(ARGV[5])

local redis_time = redis.call('TIME')
local now_seconds = tonumber(redis_time[1])
local now_ms = (now_seconds * 1000) + math.floor(tonumber(redis_time[2]) / 1000)

local kst_seconds_since_midnight = math.fmod(now_seconds + (9 * 60 * 60), 24 * 60 * 60)
local server_limit = base_limit
if peak_window_group == 1
    and kst_seconds_since_midnight >= (9 * 60 * 60)
    and kst_seconds_since_midnight < (9 * 60 * 60 + 10 * 60) then
    server_limit = 3
end
local limit = math.min(application_limit, server_limit)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now_ms, tostring(now_ms) .. ':' .. member_suffix)
    redis.call('PEXPIRE', key, window_ms)
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local wait_ms = 1
if oldest[2] then
    wait_ms = math.max((tonumber(oldest[2]) + window_ms) - now_ms, 1)
end
return {0, wait_ms}
"""


@dataclass(frozen=True)
class TossRateLimitHeaders:
    """Rate-limit metadata supplied by the Toss Open API response.

    The provider may change limits without notice.  Consumers that need a
    current cap must use ``limit`` from this response snapshot, not
    ``_BASE_LIMITS`` (which remains only a local, process-level guard for the
    established runtime clients).
    """

    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    retry_after_seconds: float | None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header case-insensitively for both httpx and plain test mappings."""

    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _nonnegative_float(value: str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0.0 else None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> TossRateLimitHeaders:
    """Parse the documented Toss rate headers without inventing a fallback cap."""

    return TossRateLimitHeaders(
        limit=_positive_int(_header_value(headers, "X-RateLimit-Limit")),
        remaining=_nonnegative_int(_header_value(headers, "X-RateLimit-Remaining")),
        reset_seconds=_nonnegative_float(_header_value(headers, "X-RateLimit-Reset")),
        retry_after_seconds=_nonnegative_float(_header_value(headers, "Retry-After")),
    )


class TossRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[TossApiGroup, deque[float]] = {
            group: deque() for group in TossApiGroup
        }
        # Per-group locks so a throttled group never head-of-line blocks another.
        self._locks: dict[TossApiGroup, asyncio.Lock] = {
            group: asyncio.Lock() for group in TossApiGroup
        }

    @staticmethod
    def limit_for(group: TossApiGroup, *, now: datetime | None = None) -> int:
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        if group in _PEAK_WINDOW_GROUPS:
            if now.hour == 9 and 0 <= now.minute < 10:
                return 3
        return _BASE_LIMITS[group]

    async def acquire(self, group: TossApiGroup) -> None:
        lock = self._locks[group]
        bucket = self._buckets[group]
        while True:
            async with lock:
                now = time.monotonic()
                while bucket and now - bucket[0] >= 1.0:
                    bucket.popleft()
                # Re-evaluate the limit each iteration so the 6->3 peak-window
                # transition cannot admit an extra call.
                limit = self.limit_for(group)
                if len(bucket) < limit:
                    bucket.append(now)
                    return
                sleep_for = max(1.0 - (now - bucket[0]), 0.0)
            # Sleep OUTSIDE the lock, then re-loop and re-check the limit.
            await asyncio.sleep(sleep_for if sleep_for > 0.0 else 0.001)


class RedisTossRateLimiter(TossRateLimiter):
    """Redis-backed, client-scoped version of the Toss sliding-window limiter.

    The Redis script is the authority for admission and time.  A Redis outage
    permanently downgrades this process instance to the existing process-local
    limiter, retaining a bounded budget instead of allowing an unthrottled
    broker request through.
    """

    def __init__(
        self,
        *,
        client_id: str,
        redis_client: Any,
        fallback_limiter: TossRateLimiter | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id must not be empty for Redis rate limiting")
        self._client_id = client_id
        self._redis_client = redis_client
        self._fallback_limiter = fallback_limiter or _get_local_shared_rate_limiter()
        self._redis_degraded = False

    def key_for(self, group: TossApiGroup) -> str:
        return toss_rate_limit_key(self._client_id, group)

    @staticmethod
    def _parse_acquire_result(result: Any) -> tuple[bool, float]:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Redis rate-limiter script returned malformed result")
        try:
            admitted = int(result[0])
            wait_milliseconds = float(result[1])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Redis rate-limiter script returned non-numeric result"
            ) from exc
        if admitted not in {0, 1}:
            raise RuntimeError("Redis rate-limiter script returned invalid admission")
        if admitted == 0 and wait_milliseconds <= 0.0:
            raise RuntimeError("Redis rate-limiter script returned invalid wait")
        return admitted == 1, wait_milliseconds / 1_000.0

    async def acquire(self, group: TossApiGroup) -> None:
        if self._redis_degraded:
            await self._fallback_limiter.acquire(group)
            return

        while True:
            # Preserve the local limiter's invariant: re-evaluate on every
            # retry rather than keeping a stale 6 TPS value across 09:00 KST.
            application_limit = self.limit_for(group)
            try:
                result = await self._redis_client.eval(
                    _REDIS_SLIDING_WINDOW_LUA,
                    1,
                    self.key_for(group),
                    application_limit,
                    _BASE_LIMITS[group],
                    int(group in _PEAK_WINDOW_GROUPS),
                    uuid.uuid4().hex,
                    _REDIS_WINDOW_MILLISECONDS,
                )
                admitted, wait_seconds = self._parse_acquire_result(result)
            except Exception as exc:  # noqa: BLE001 -- local fallback is fail-closed
                self._redis_degraded = True
                _warn_redis_fallback_once(exc)
                await self._fallback_limiter.acquire(group)
                return

            if admitted:
                return
            # Redis returns the earliest useful retry delay. Sleep outside any
            # local lock, then re-check both Redis TIME and ``limit_for``.
            await asyncio.sleep(max(wait_seconds, 0.001))


_shared_rate_limiter: TossRateLimiter | None = None
_local_shared_rate_limiter: TossRateLimiter | None = None
_shared_rate_limiter_redis_client: Any | None = None
_last_redis_fallback_warning_at: float | None = None


class _RedisRateLimiterUnavailable(RuntimeError):
    """Internal sentinel used when the Redis opt-in cannot be configured."""


def _get_local_shared_rate_limiter() -> TossRateLimiter:
    global _local_shared_rate_limiter
    if _local_shared_rate_limiter is None:
        _local_shared_rate_limiter = TossRateLimiter()
    return _local_shared_rate_limiter


def _warn_redis_fallback_once(exc: BaseException) -> None:
    """Log only a bounded, non-secret fallback signal for Redis failures."""

    global _last_redis_fallback_warning_at
    now = time.monotonic()
    if (
        _last_redis_fallback_warning_at is not None
        and now - _last_redis_fallback_warning_at
        < _REDIS_FALLBACK_WARNING_INTERVAL_SECONDS
    ):
        return
    _last_redis_fallback_warning_at = now
    logger.warning(
        "Toss Redis rate limiter unavailable; falling back to process-local limiter (%s)",
        type(exc).__name__,
    )


def _build_redis_client() -> redis.Redis:
    return redis.from_url(
        settings.get_redis_url(),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        decode_responses=True,
    )


async def _drain_redis_close(awaitable: Any) -> None:
    with contextlib.suppress(Exception):
        await awaitable


def _close_owned_redis_client(client: Any) -> None:
    """Release a factory-created Redis pool without making reset asynchronous."""

    if client is None:
        return
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        result = closer()
    except Exception:  # noqa: BLE001 -- teardown must not mask test cleanup
        return
    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with contextlib.suppress(Exception):
            asyncio.run(_drain_redis_close(result))
        return
    loop.create_task(_drain_redis_close(result))


def get_shared_rate_limiter() -> TossRateLimiter:
    """Return the configured shared limiter without changing existing callers.

    ``local`` is the default and preserves the existing process-global
    singleton.  ``redis`` is an explicit opt-in that scopes the shared window
    to the Toss OAuth client id; setup failures safely retain the local budget.
    """

    global _shared_rate_limiter, _shared_rate_limiter_redis_client
    if _shared_rate_limiter is None:
        backend = (
            str(getattr(settings, "toss_rate_limiter_backend", "local")).strip().lower()
        )
        if backend != "redis":
            _shared_rate_limiter = _get_local_shared_rate_limiter()
            return _shared_rate_limiter

        client_id = str(getattr(settings, "toss_api_client_id", "") or "")
        redis_client: Any | None = None
        try:
            if not client_id.strip():
                raise _RedisRateLimiterUnavailable(
                    "TOSS_API_CLIENT_ID is required for the Redis limiter namespace"
                )
            redis_client = _build_redis_client()
            _shared_rate_limiter = RedisTossRateLimiter(
                client_id=client_id,
                redis_client=redis_client,
                fallback_limiter=_get_local_shared_rate_limiter(),
            )
            _shared_rate_limiter_redis_client = redis_client
        except Exception as exc:  # noqa: BLE001 -- Redis opt-in must fail closed
            _close_owned_redis_client(redis_client)
            _warn_redis_fallback_once(exc)
            _shared_rate_limiter = _get_local_shared_rate_limiter()
    return _shared_rate_limiter


def reset_shared_rate_limiter() -> None:
    """Test hook: drop local/Redis singleton state so suites start clean."""

    global _last_redis_fallback_warning_at
    global _local_shared_rate_limiter
    global _shared_rate_limiter
    global _shared_rate_limiter_redis_client
    _shared_rate_limiter = None
    _local_shared_rate_limiter = None
    redis_client = _shared_rate_limiter_redis_client
    _shared_rate_limiter_redis_client = None
    _last_redis_fallback_warning_at = None
    _close_owned_redis_client(redis_client)


def retry_delay_seconds(
    retry_after: str | None, *, attempt: int, jitter: float | None = None
) -> float:
    try:
        if retry_after is not None:
            return max(float(retry_after), 0.0)
    except ValueError:
        pass
    base = min(2.0**attempt, 16.0)
    return base + (random.uniform(0.0, base) if jitter is None else jitter)

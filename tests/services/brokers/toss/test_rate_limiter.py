from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Generator
from datetime import datetime
from shutil import which
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import redis as sync_redis
import redis.asyncio as redis

from app.services.brokers.toss import rate_limiter as rate_limiter_module
from app.services.brokers.toss.rate_limiter import (
    RedisTossRateLimiter,
    TossApiGroup,
    TossRateLimiter,
    TossRateLimitHeaders,
    get_shared_rate_limiter,
    parse_rate_limit_headers,
    retry_delay_seconds,
    toss_rate_limit_key,
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _redis_ping(url: str) -> bool:
    client = sync_redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001 -- fixture reports the required test service
        return False
    finally:
        client.close()


@pytest.fixture(scope="module")
def redis_limiter_url() -> Generator[str]:
    """Provide a real Redis because fakeredis lacks Lua/EVAL in this project.

    A disposable local server is preferred. GitHub CI falls back to its existing
    Redis service on an isolated DB and fails loudly if neither is available.
    """

    redis_server = which("redis-server")
    if redis_server is None:
        service_url = os.environ.get(
            "ROB892_TEST_REDIS_URL", "redis://localhost:6379/15"
        )
        if not _redis_ping(service_url):
            pytest.fail(
                "Redis Lua rate-limiter tests require redis-server or the CI Redis service"
            )
        yield service_url
        return

    port = _free_tcp_port()
    process = subprocess.Popen(
        [
            redis_server,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--loglevel",
            "warning",
            "--protected-mode",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"redis://127.0.0.1:{port}/0"
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("redis-server exited before rate-limiter tests started")
            if _redis_ping(url):
                break
            time.sleep(0.05)
        else:
            pytest.fail("redis-server did not become ready for rate-limiter tests")
        yield url
    finally:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)
            if process.poll() is None:
                process.kill()


async def _assert_task_is_throttled(task: asyncio.Task[None]) -> None:
    try:
        await asyncio.sleep(0.05)
        assert not task.done(), "request bypassed the configured rate limit"
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_order_info_peak_limit_is_three_tps() -> None:
    now = datetime(2026, 6, 12, 9, 5, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 3


def test_order_info_normal_limit_is_six_tps() -> None:
    now = datetime(2026, 6, 12, 9, 11, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 6


def test_market_data_limit_is_ten_tps() -> None:
    limiter = TossRateLimiter()

    assert limiter.limit_for(TossApiGroup.MARKET_DATA) == 10


def test_rate_limit_headers_are_parsed_case_insensitively_without_fallback_cap() -> (
    None
):
    headers = parse_rate_limit_headers(
        {
            "x-ratelimit-limit": "7",
            "X-RateLimit-Remaining": "2",
            "X-RATELIMIT-RESET": "0.25",
            "retry-after": "3",
        }
    )

    assert headers == TossRateLimitHeaders(
        limit=7,
        remaining=2,
        reset_seconds=0.25,
        retry_after_seconds=3.0,
    )


def test_rate_limit_headers_leave_malformed_or_missing_cap_unknown() -> None:
    headers = parse_rate_limit_headers(
        {
            "X-RateLimit-Limit": "not-a-number",
            "X-RateLimit-Remaining": "-1",
            "X-RateLimit-Reset": "bad",
            "Retry-After": "-2",
        }
    )

    assert headers == TossRateLimitHeaders(None, None, None, None)


@pytest.mark.parametrize(
    ("retry_after", "attempt", "expected_min"),
    [("2", 0, 2.0), (None, 2, 4.0), ("bad", 1, 2.0)],
)
def test_retry_delay_seconds_uses_header_or_backoff(
    retry_after: str | None, attempt: int, expected_min: float
) -> None:
    delay = retry_delay_seconds(retry_after, attempt=attempt, jitter=0.0)

    assert delay == expected_min


def test_get_shared_rate_limiter_is_process_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-547: every call site shares one limiter so group TPS holds across
    concurrent clients within a process."""
    rate_limiter_module.reset_shared_rate_limiter()
    monkeypatch.setattr(
        rate_limiter_module.settings, "toss_rate_limiter_backend", "local"
    )
    try:
        first = get_shared_rate_limiter()
        second = get_shared_rate_limiter()

        assert first is second
        assert type(first) is TossRateLimiter
    finally:
        rate_limiter_module.reset_shared_rate_limiter()


def test_get_shared_rate_limiter_selects_redis_only_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnusedRedis:
        def close(self) -> None:
            return None

    redis_client = _UnusedRedis()
    rate_limiter_module.reset_shared_rate_limiter()
    monkeypatch.setattr(
        rate_limiter_module.settings, "toss_rate_limiter_backend", "redis"
    )
    monkeypatch.setattr(
        rate_limiter_module.settings, "toss_api_client_id", "factory-client-id"
    )
    monkeypatch.setattr(
        rate_limiter_module, "_build_redis_client", lambda: redis_client
    )
    try:
        limiter = get_shared_rate_limiter()

        assert isinstance(limiter, RedisTossRateLimiter)
        assert limiter.key_for(TossApiGroup.AUTH) == toss_rate_limit_key(
            "factory-client-id", TossApiGroup.AUTH
        )
    finally:
        rate_limiter_module.reset_shared_rate_limiter()


def test_redis_key_uses_only_client_fingerprint_and_group() -> None:
    client_id = "client-id-for-key-contract"
    expected_fingerprint = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]

    assert toss_rate_limit_key(client_id, TossApiGroup.AUTH) == (
        f"toss:ratelimit:{expected_fingerprint}:AUTH"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_instances_share_one_client_scoped_five_tps_budget(
    redis_limiter_url: str,
) -> None:
    """Two instances model two workers: a PID in this key must turn this red."""

    first_client = redis.from_url(redis_limiter_url, decode_responses=True)
    second_client = redis.from_url(redis_limiter_url, decode_responses=True)
    client_id = f"shared-budget-{uuid.uuid4().hex}"
    key = toss_rate_limit_key(client_id, TossApiGroup.AUTH)
    first = RedisTossRateLimiter(client_id=client_id, redis_client=first_client)
    second = RedisTossRateLimiter(client_id=client_id, redis_client=second_client)
    try:
        await first_client.delete(key)
        assert first.key_for(TossApiGroup.AUTH) == key
        assert second.key_for(TossApiGroup.AUTH) == key

        acquires = [first.acquire(TossApiGroup.AUTH) for _ in range(3)]
        acquires.extend(second.acquire(TossApiGroup.AUTH) for _ in range(2))
        await asyncio.gather(*acquires)

        assert await first_client.zcard(key) == 5
        sixth = asyncio.create_task(second.acquire(TossApiGroup.AUTH))
        await _assert_task_is_throttled(sixth)
    finally:
        await first_client.delete(key)
        await first_client.aclose()
        await second_client.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_order_peak_window_enforces_three_tps(
    monkeypatch: pytest.MonkeyPatch,
    redis_limiter_url: str,
) -> None:
    peak = datetime(2026, 6, 12, 9, 9, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    after_peak = datetime(2026, 6, 12, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
    assert RedisTossRateLimiter.limit_for(TossApiGroup.ORDER, now=peak) == 3
    assert RedisTossRateLimiter.limit_for(TossApiGroup.ORDER, now=after_peak) == 6

    def _peak_limit(group: TossApiGroup, *, now: datetime | None = None) -> int:
        del now
        return TossRateLimiter.limit_for(group, now=peak)

    monkeypatch.setattr(RedisTossRateLimiter, "limit_for", staticmethod(_peak_limit))
    redis_client = redis.from_url(redis_limiter_url, decode_responses=True)
    client_id = f"peak-window-{uuid.uuid4().hex}"
    key = toss_rate_limit_key(client_id, TossApiGroup.ORDER)
    limiter = RedisTossRateLimiter(client_id=client_id, redis_client=redis_client)
    try:
        await redis_client.delete(key)
        await asyncio.gather(*(limiter.acquire(TossApiGroup.ORDER) for _ in range(3)))

        assert await redis_client.zcard(key) == 3
        fourth = asyncio.create_task(limiter.acquire(TossApiGroup.ORDER))
        await _assert_task_is_throttled(fourth)
    finally:
        await redis_client.delete(key)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_redis_timeout_downgrades_to_local_without_fail_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _TimedOutRedis:
        def __init__(self) -> None:
            self.calls = 0

        async def eval(self, *args: Any) -> list[int]:
            del args
            self.calls += 1
            raise TimeoutError("unavailable")

    rate_limiter_module.reset_shared_rate_limiter()
    caplog.set_level(logging.WARNING, logger=rate_limiter_module.__name__)
    unavailable = _TimedOutRedis()
    fallback = TossRateLimiter()
    limiter = RedisTossRateLimiter(
        client_id="timeout-client-id",
        redis_client=unavailable,
        fallback_limiter=fallback,
    )
    try:
        await limiter.acquire(TossApiGroup.ACCOUNT)
        second = asyncio.create_task(limiter.acquire(TossApiGroup.ACCOUNT))
        await _assert_task_is_throttled(second)

        assert unavailable.calls == 1
        warnings = [
            record
            for record in caplog.records
            if "falling back to process-local limiter" in record.getMessage()
        ]
        assert len(warnings) == 1
    finally:
        rate_limiter_module.reset_shared_rate_limiter()


@pytest.mark.asyncio
async def test_throttled_group_does_not_block_other_groups() -> None:
    """ROB-547: a saturated ORDER bucket must not head-of-line block a
    MARKET_DATA read (per-group locking, sleep outside the global lock)."""
    limiter = TossRateLimiter()
    # Saturate ORDER (peak/normal limit >= 6) so the next ORDER acquire sleeps ~1s.
    for _ in range(TossRateLimiter.limit_for(TossApiGroup.ORDER)):
        await limiter.acquire(TossApiGroup.ORDER)

    async def slow_order() -> None:
        await limiter.acquire(TossApiGroup.ORDER)

    order_task = asyncio.create_task(slow_order())
    await asyncio.sleep(0.05)  # let the ORDER acquire enter its throttle wait

    start = time.monotonic()
    await limiter.acquire(TossApiGroup.MARKET_DATA)
    elapsed = time.monotonic() - start

    assert elapsed < 0.2, "MARKET_DATA was blocked behind the throttled ORDER group"
    order_task.cancel()

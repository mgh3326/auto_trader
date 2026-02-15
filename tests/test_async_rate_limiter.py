"""Unit tests for AsyncSlidingWindowRateLimiter."""

import asyncio
import time
from pathlib import Path

import pytest

from app.core.async_rate_limiter import (
    AsyncSlidingWindowRateLimiter,
    get_all_limiters,
    get_limiter,
    reset_limiters,
)


class TestAsyncSlidingWindowRateLimiter:
    """Tests for AsyncSlidingWindowRateLimiter."""

    def test_init_validates_rate(self):
        with pytest.raises(ValueError, match="rate must be positive"):
            AsyncSlidingWindowRateLimiter(rate=0, period=1.0)

        with pytest.raises(ValueError, match="rate must be positive"):
            AsyncSlidingWindowRateLimiter(rate=-1, period=1.0)

    def test_init_validates_period(self):
        with pytest.raises(ValueError, match="period must be positive"):
            AsyncSlidingWindowRateLimiter(rate=10, period=0)

        with pytest.raises(ValueError, match="period must be positive"):
            AsyncSlidingWindowRateLimiter(rate=10, period=-1.0)

    @pytest.mark.asyncio
    async def test_acquire_below_limit_returns_immediately(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=5, period=1.0, name="test")

        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, "Should acquire 5 requests under limit instantly"
        stats = limiter.get_stats()
        assert stats["total_requests"] == 5
        assert stats["throttled_requests"] == 0

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_limit_exceeded(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=3, period=0.3, name="test")

        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.25, "Should wait for window to reset"
        stats = limiter.get_stats()
        assert stats["throttled_requests"] >= 1

    @pytest.mark.asyncio
    async def test_sliding_window_resets_after_period(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=2, period=0.2, name="test")

        await limiter.acquire()
        await limiter.acquire()

        await asyncio.sleep(0.25)

        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start

        assert elapsed < 0.05, "Should acquire immediately after window reset"

    @pytest.mark.asyncio
    async def test_blocking_callback_invoked(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=1, period=0.2, name="test")

        callback_wait_times: list[float] = []

        async def callback(wait_time: float):
            callback_wait_times.append(wait_time)

        await limiter.acquire()
        await limiter.acquire(blocking_callback=callback)

        assert len(callback_wait_times) == 1
        assert callback_wait_times[0] > 0

    @pytest.mark.asyncio
    async def test_sync_blocking_callback_supported(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=1, period=0.2, name="test")

        callback_invoked = False

        def sync_callback(wait_time: float):
            nonlocal callback_invoked
            callback_invoked = True

        await limiter.acquire()
        await limiter.acquire(blocking_callback=sync_callback)

        assert callback_invoked

    @pytest.mark.asyncio
    async def test_concurrent_acquire_respects_limit(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=5, period=0.5, name="test")

        async def acquire_one():
            await limiter.acquire()
            return time.monotonic()

        start = time.monotonic()
        _results = await asyncio.gather(*[acquire_one() for _ in range(10)])
        total_time = time.monotonic() - start

        assert total_time >= 0.4, "Concurrent requests should be throttled"

        stats = limiter.get_stats()
        assert stats["total_requests"] == 10
        assert stats["throttled_requests"] >= 1

    def test_get_stats_returns_correct_structure(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=10, period=1.0, name="test_stats")

        stats = limiter.get_stats()

        assert stats["name"] == "test_stats"
        assert stats["rate"] == 10
        assert stats["period"] == 1.0
        assert stats["total_requests"] == 0
        assert stats["throttled_requests"] == 0
        assert stats["throttle_rate"] == 0.0
        assert stats["total_wait_time"] == 0.0
        assert stats["avg_wait_time"] == 0.0
        assert stats["current_window_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_track_throttle_rate(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=1, period=0.2, name="test")

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        stats = limiter.get_stats()
        assert stats["total_requests"] == 3
        assert stats["throttled_requests"] >= 1
        assert stats["throttle_rate"] > 0

    def test_reset_stats_clears_counters(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=10, period=1.0, name="test")

        limiter._total_requests = 100
        limiter._throttled_requests = 50
        limiter._total_wait_time = 10.0

        limiter.reset_stats()

        assert limiter._total_requests == 0
        assert limiter._throttled_requests == 0
        assert limiter._total_wait_time == 0.0

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_acquire(self):
        limiter = AsyncSlidingWindowRateLimiter(rate=1, period=0.2, name="test")

        async def bad_callback(wait_time: float):
            raise RuntimeError("Callback error")

        await limiter.acquire()
        result = await limiter.acquire(blocking_callback=bad_callback)

        assert result is True


class TestGlobalLimiters:
    """Tests for global rate limiter management."""

    def test_reset_limiters(self):
        reset_limiters()


class TestPerApiRateLimiters:
    """Tests for per-API rate limiter registry."""

    def setup_method(self):
        """Reset limiters before each test."""
        reset_limiters()

    @pytest.mark.asyncio
    async def test_get_limiter_creates_new_instance(self):
        limiter = await get_limiter("kis", "TEST_TR|/test/path")
        assert limiter is not None
        assert "TEST_TR|/test/path" in limiter.name

    @pytest.mark.asyncio
    async def test_get_limiter_returns_same_instance_for_same_key(self):
        limiter1 = await get_limiter("kis", "SAME_KEY")
        limiter2 = await get_limiter("kis", "SAME_KEY")
        assert limiter1 is limiter2

    @pytest.mark.asyncio
    async def test_get_limiter_creates_different_instances_for_different_keys(self):
        limiter1 = await get_limiter("kis", "KEY1")
        limiter2 = await get_limiter("kis", "KEY2")
        assert limiter1 is not limiter2

    @pytest.mark.asyncio
    async def test_get_limiter_uses_custom_rate_and_period(self):
        limiter = await get_limiter("kis", "CUSTOM_RATE", rate=5, period=2.0)
        assert limiter.rate == 5
        assert limiter.period == 2.0

    @pytest.mark.asyncio
    async def test_get_limiter_uses_default_when_not_specified(self):
        limiter = await get_limiter("kis", "DEFAULT_RATE")
        assert limiter.rate == 19  # Default for KIS
        assert limiter.period == 1.0

    @pytest.mark.asyncio
    async def test_get_all_limiters_returns_copy(self):
        await get_limiter("kis", "TEST1")
        await get_limiter("upbit", "TEST2")
        all_limiters = get_all_limiters()
        assert "kis|TEST1" in all_limiters
        assert "upbit|TEST2" in all_limiters

    @pytest.mark.asyncio
    async def test_per_api_limiters_are_independent(self):
        limiter1 = await get_limiter("kis", "API1", rate=2, period=0.2)
        limiter2 = await get_limiter("kis", "API2", rate=10, period=1.0)

        # Exhaust limiter1
        await limiter1.acquire()
        await limiter1.acquire()

        # limiter2 should still be able to acquire immediately
        start = time.monotonic()
        await limiter2.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, "Second limiter should not be throttled"

        # limiter1 should be throttled
        start = time.monotonic()
        await limiter1.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15, "First limiter should be throttled"

    @pytest.mark.asyncio
    async def test_concurrent_get_limiter_is_thread_safe(self):
        async def create_limiter(key_suffix: int):
            return await get_limiter("kis", f"CONCURRENT_{key_suffix}")

        await asyncio.gather(*[create_limiter(i) for i in range(10)])
        all_limiters = get_all_limiters()

        # All limiters should be created
        for i in range(10):
            assert f"kis|CONCURRENT_{i}" in all_limiters

    @pytest.mark.asyncio
    async def test_reset_limiters_clears_registry(self):
        await get_limiter("kis", "BEFORE_RESET")
        assert "kis|BEFORE_RESET" in get_all_limiters()

        reset_limiters()

        assert "kis|BEFORE_RESET" not in get_all_limiters()
        assert len(get_all_limiters()) == 0


class TestKisServiceRateLimitWiring:
    """Static guardrails for KIS rate-limit wrapper coverage."""

    def test_kis_uses_async_client_only_in_token_and_wrapper(self):
        content = Path("app/services/kis.py").read_text(encoding="utf-8")
        assert content.count("async with httpx.AsyncClient") == 2

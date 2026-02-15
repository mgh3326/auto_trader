"""Async Sliding-Window Rate Limiter

Implements a true sliding-window rate limiter using a deque of timestamps.
Unlike fixed-window limiters, this provides accurate rate limiting without
burst issues at window boundaries.

Usage:
    limiter = AsyncSlidingWindowRateLimiter(rate=19, period=1.0, name="kis")

    # Before API call
    await limiter.acquire()

    # With callback for logging
    await limiter.acquire(blocking_callback=lambda wait: logger.info(f"Waiting {wait:.2f}s"))

Per-API Rate Limiting:
    # Get limiter for specific API endpoint
    limiter = await get_limiter("kis", "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice")

    # For Upbit
    limiter = await get_limiter("upbit", "GET /v1/ticker")
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class RateLimitExceededError(RuntimeError):
    """Raised when retry budget for a rate-limited request is exhausted."""


class AsyncSlidingWindowRateLimiter:
    """
    Async sliding-window rate limiter.

    Uses a deque to track request timestamps within the sliding window.
    When the rate limit is exceeded, it waits until the oldest request
    falls outside the window before allowing the next request.

    Attributes:
        rate: Maximum number of requests allowed per period.
        period: Time window in seconds.
        name: Human-readable name for logging purposes.
    """

    __slots__ = [
        "rate",
        "period",
        "name",
        "_timestamps",
        "_lock",
        "_total_requests",
        "_throttled_requests",
        "_total_wait_time",
    ]

    def __init__(self, rate: int, period: float, name: str = "default"):
        """
        Initialize the rate limiter.

        Args:
            rate: Maximum number of requests allowed per period.
            period: Time window in seconds.
            name: Human-readable name for logging.
        """
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if period <= 0:
            raise ValueError(f"period must be positive, got {period}")

        self.rate = rate
        self.period = period
        self.name = name

        # Deque of timestamps for requests in the current window
        self._timestamps: deque[float] = deque(maxlen=rate + 10)

        # Async lock for thread safety
        self._lock = asyncio.Lock()

        # Statistics
        self._total_requests = 0
        self._throttled_requests = 0
        self._total_wait_time = 0.0

    async def acquire(
        self,
        blocking_callback: Callable[[float], Awaitable[None] | None] | None = None,
    ) -> bool:
        """
        Acquire permission to make a request.

        If the rate limit has been reached, this method will block until
        the oldest request falls outside the sliding window.

        Args:
            blocking_callback: Optional async or sync callback invoked when
                throttling occurs. Receives the wait time in seconds.
                Useful for logging or metrics.

        Returns:
            True if the request was allowed (always True for blocking mode).

        Raises:
            RuntimeError: If the wait time calculation fails unexpectedly.
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                window_start = now - self.period

                # Remove timestamps outside the current window (sliding window)
                while self._timestamps and self._timestamps[0] < window_start:
                    self._timestamps.popleft()

                # Check if we're at rate limit
                if len(self._timestamps) >= self.rate:
                    # Calculate wait time until oldest request exits window
                    oldest = self._timestamps[0]
                    wait_time = oldest + self.period - now + 0.05  # 50ms buffer

                    if wait_time > 0:
                        self._throttled_requests += 1
                        self._total_wait_time += wait_time

                        logger.warning(
                            "[%s] Rate limit reached (%d/%.1fs), waiting %.3fs",
                            self.name,
                            self.rate,
                            self.period,
                            wait_time,
                        )

                        # Release lock during callback and sleep
                        if blocking_callback is not None:
                            try:
                                result = blocking_callback(wait_time)
                                if asyncio.iscoroutine(result):
                                    # Release lock before awaiting callback
                                    self._lock.release()
                                    try:
                                        await result
                                    finally:
                                        await self._lock.acquire()
                            except Exception as e:
                                logger.error(
                                    "[%s] blocking_callback error: %s", self.name, e
                                )

                        # Sleep outside the lock to allow other waiters
                        self._lock.release()
                        try:
                            await asyncio.sleep(wait_time)
                        finally:
                            await self._lock.acquire()

                        # Loop back to re-check window after sleep
                        continue

                # Record this request
                self._timestamps.append(now)
                self._total_requests += 1

                return True

    def get_stats(self) -> dict:
        """
        Get rate limiter statistics.

        Returns:
            Dictionary with:
            - total_requests: Total number of acquire() calls
            - throttled_requests: Number of requests that were throttled
            - throttle_rate: Percentage of requests throttled
            - total_wait_time: Total time spent waiting in seconds
            - avg_wait_time: Average wait time per throttled request
            - current_window_count: Requests in current sliding window
        """
        throttle_rate = (
            (self._throttled_requests / self._total_requests * 100)
            if self._total_requests > 0
            else 0.0
        )
        avg_wait_time = (
            self._total_wait_time / self._throttled_requests
            if self._throttled_requests > 0
            else 0.0
        )

        return {
            "name": self.name,
            "rate": self.rate,
            "period": self.period,
            "total_requests": self._total_requests,
            "throttled_requests": self._throttled_requests,
            "throttle_rate": round(throttle_rate, 2),
            "total_wait_time": round(self._total_wait_time, 3),
            "avg_wait_time": round(avg_wait_time, 3),
            "current_window_count": len(self._timestamps),
        }

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._total_requests = 0
        self._throttled_requests = 0
        self._total_wait_time = 0.0


# Per-API rate limiter registry (lazy initialization)
_limiters: dict[str, AsyncSlidingWindowRateLimiter] = {}
_limiters_lock = asyncio.Lock()

# Default rate limits per provider
DEFAULT_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "kis": (19, 1.0),  # 19 requests per second
    "upbit": (10, 1.0),  # 10 requests per second
}


async def get_limiter(
    provider: str,
    api_key: str,
    rate: int | None = None,
    period: float | None = None,
) -> AsyncSlidingWindowRateLimiter:
    """
    Get or create a rate limiter for a specific API endpoint.

    This function maintains a registry of rate limiters keyed by
    "{provider}|{api_key}". If a limiter doesn't exist, it creates one
    with the specified or default rate limits.

    Args:
        provider: Provider name ("kis" or "upbit")
        api_key: API-specific key (e.g., "TR_ID|/path" for KIS, "METHOD /path" for Upbit)
        rate: Maximum requests per period (uses provider default if None)
        period: Time window in seconds (uses provider default if None)

    Returns:
        AsyncSlidingWindowRateLimiter instance for the specified API

    Example:
        # KIS per-API limiter
        limiter = await get_limiter("kis", "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice")

        # Upbit per-API limiter
        limiter = await get_limiter("upbit", "GET /v1/ticker")
    """
    registry_key = f"{provider}|{api_key}"

    # Fast path: limiter already exists
    if registry_key in _limiters:
        return _limiters[registry_key]

    async with _limiters_lock:
        # Double-check after acquiring lock
        if registry_key in _limiters:
            return _limiters[registry_key]

        # Use provided rate/period or fall back to provider defaults
        if rate is None or period is None:
            default_rate, default_period = DEFAULT_RATE_LIMITS.get(
                provider,
                (19, 1.0),  # Safe fallback
            )
            rate = rate if rate is not None else default_rate
            period = period if period is not None else default_period

        limiter = AsyncSlidingWindowRateLimiter(
            rate=rate,
            period=period,
            name=registry_key,
        )
        _limiters[registry_key] = limiter
        return limiter


async def get_kis_limiter() -> AsyncSlidingWindowRateLimiter:
    """
    Get or create the global KIS rate limiter (legacy compatibility).

    Uses settings from app.core.config for rate and period.
    Prefer using get_limiter("kis", api_key) for per-API rate limiting.
    """
    return await get_limiter("kis", "_global")


async def get_upbit_limiter() -> AsyncSlidingWindowRateLimiter:
    """
    Get or create the global Upbit rate limiter (legacy compatibility).

    Uses settings from app.core.config for rate and period.
    Prefer using get_limiter("upbit", api_key) for per-API rate limiting.
    """
    return await get_limiter("upbit", "_global")


def reset_limiters() -> None:
    """
    Reset all rate limiter instances.

    Used primarily for testing to ensure clean state between tests.
    """
    global _limiters
    _limiters = {}


def get_all_limiters() -> dict[str, AsyncSlidingWindowRateLimiter]:
    """
    Get a copy of all registered rate limiters.

    Useful for monitoring and diagnostics.
    """
    return _limiters.copy()

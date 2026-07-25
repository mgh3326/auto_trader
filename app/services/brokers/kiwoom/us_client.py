"""US-credential factory for the mock-host-guarded Kiwoom transport."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Self

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomMockClient,
)


class _PerTrRateLimiter:
    """Serialize one mock US dispatch per API ID per second."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval_seconds: float = 1.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._clock = clock
        self._sleep = sleep
        self._interval_seconds = interval_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_dispatch: dict[str, float] = {}

    async def wait(self, api_id: str) -> None:
        lock = self._locks.get(api_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[api_id] = lock
        async with lock:
            now = self._clock()
            last_dispatch = self._last_dispatch.get(api_id)
            if last_dispatch is not None:
                remaining = self._interval_seconds - (now - last_dispatch)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_dispatch[api_id] = self._clock()


class KiwoomMockUsClient(KiwoomMockClient):
    """Kiwoom mock client constructed exclusively from US credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        account_no: str,
        timeout: float = constants.DEFAULT_TIMEOUT,
        rate_limit_clock: Callable[[], float] = time.monotonic,
        rate_limit_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            base_url=base_url,
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            timeout=timeout,
        )
        self._tr_rate_limiter = _PerTrRateLimiter(
            clock=rate_limit_clock,
            sleep=rate_limit_sleep,
        )

    async def _before_api_dispatch(self, api_id: str) -> None:
        await self._tr_rate_limiter.wait(api_id)

    @classmethod
    def from_app_settings(cls) -> Self:
        from app.core.config import settings, validate_kiwoom_mock_us_config

        missing = validate_kiwoom_mock_us_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom US mock account is disabled or missing required "
                "configuration: " + ", ".join(missing)
            )
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_us_app_key),
            app_secret=str(settings.kiwoom_mock_us_app_secret),
            account_no=str(settings.kiwoom_mock_us_account_no),
        )

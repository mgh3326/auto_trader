"""US-credential factory for the mock-host-guarded Kiwoom transport."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Self

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomMockClient,
    assert_distinct_kr_us_identities,
)


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
        redis_client: Any | None = None,
        redis_settings: Any | None = None,
        rate_limit_clock: Callable[[], float] = time.monotonic,
        rate_limit_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            base_url=base_url,
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            timeout=timeout,
            redis_client=redis_client,
            redis_settings=redis_settings,
            rate_limit_clock=rate_limit_clock,
            rate_limit_sleep=rate_limit_sleep,
        )

    @classmethod
    def from_app_settings(cls) -> Self:
        from app.core.config import settings, validate_kiwoom_mock_us_config

        missing = validate_kiwoom_mock_us_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom US mock account is disabled or missing required "
                "configuration: " + ", ".join(missing)
            )
        assert_distinct_kr_us_identities(settings)
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_us_app_key),
            app_secret=str(settings.kiwoom_mock_us_app_secret),
            account_no=str(settings.kiwoom_mock_us_account_no),
            redis_settings=settings,
        )

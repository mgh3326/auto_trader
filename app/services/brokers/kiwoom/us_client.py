"""US-credential factory for the mock-host-guarded Kiwoom transport."""

from __future__ import annotations

from typing import Self

from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomMockClient,
)


class KiwoomMockUsClient(KiwoomMockClient):
    """Kiwoom mock client constructed exclusively from US credentials."""

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

# app/services/brokers/kiwoom/client.py
"""Kiwoom mock-only REST client.

Mock-only by construction: any base URL other than the configured mock host
raises ``KiwoomEndpointError`` so misconfiguration cannot reach the live
broker. ``from_app_settings`` aggregates config validation via
``validate_kiwoom_mock_config`` and refuses to construct a client until all
required env values are present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services.brokers.kiwoom import constants

if TYPE_CHECKING:
    pass


class KiwoomConfigurationError(RuntimeError):
    """Raised when Kiwoom mock config is incomplete or disabled."""


class KiwoomEndpointError(RuntimeError):
    """Raised when a non-mock base URL would be used."""


@dataclass(frozen=True)
class KiwoomMockClient:
    base_url: str
    app_key: str
    app_secret: str
    account_no: str

    def __post_init__(self) -> None:
        if str(self.base_url).rstrip("/") != constants.MOCK_BASE_URL:
            raise KiwoomEndpointError(
                "KiwoomMockClient only accepts the mock base URL "
                f"({constants.MOCK_BASE_URL}); refusing to use {self.base_url!r}."
            )

    @classmethod
    def from_app_settings(cls) -> "KiwoomMockClient":
        from app.core.config import settings, validate_kiwoom_mock_config

        missing = validate_kiwoom_mock_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom mock account is disabled or missing required configuration: "
                + ", ".join(missing)
            )
        # Validator above guarantees these are non-empty strings at runtime.
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_app_key),
            app_secret=str(settings.kiwoom_mock_app_secret),
            account_no=str(settings.kiwoom_mock_account_no),
        )

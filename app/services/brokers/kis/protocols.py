"""Protocol definitions for KIS client interfaces.

This module defines Protocol classes that enable proper type checking
between the facade client and its sub-clients without circular imports.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from app.services.redis_token_manager import RedisTokenManager


@runtime_checkable
class KISClientProtocol(Protocol):
    """Protocol defining the interface that sub-clients expect from their parent.

    This protocol enables type-safe access to parent client functionality
    without requiring sub-clients to import the concrete KISClient class,
    avoiding circular import issues.
    """

    # Header configuration
    _hdr_base: dict[str, str]

    # Token manager for authentication
    _token_manager: RedisTokenManager

    # HTTP client lifecycle
    _http_client: httpx.AsyncClient | None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        ...

    async def _ensure_token(self) -> None:
        """Ensure valid access token is available."""
        ...

    async def _fetch_token(self) -> tuple[str, int]:
        """Fetch new OAuth2 token from KIS API."""
        ...

    async def _request_with_rate_limit(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request with rate limiting and retry logic."""
        ...

    def _get_rate_limit_for_api(self, api_key: str) -> tuple[int, float]:
        """Get rate limit for a specific API key."""
        ...

    def _kis_url(self, path: str) -> str:
        """Build a KIS API URL for the active account mode."""
        ...

    @property
    def _settings(self) -> Any:
        """Access to application settings."""
        ...


__all__ = ["KISClientProtocol"]

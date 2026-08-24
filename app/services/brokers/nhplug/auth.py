"""The sole NHPLUG production-host owner, limited to two OAuth paths.

The production OAuth host is an unavoidable vendor exception: tokens are issued
there even when every subsequent data request uses the mock host.  This module
therefore owns that host physically and permits only token issuance and token
revocation.  No data client imports this module.
"""

from __future__ import annotations

import time
from typing import Any, Final

import httpx

from app.services.brokers.nhplug.errors import (
    NHPlugMockConfigurationError,
    NHPlugMockEndpointError,
    NHPlugMockResponseError,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled

# Keep the production hostname in this file only.  The data client has an exact
# mock allowlist and deliberately does not import this module.
AUTH_BASE_URL: Final[str] = "https://api.nhplug.com:8443"
AUTH_HOST: Final[str] = "api.nhplug.com"
AUTH_PORT: Final[int] = 8443
AUTH_TOKEN_PATH: Final[str] = "/oauth2/token"
AUTH_REVOKE_PATH: Final[str] = "/oauth2/revoke"
AUTH_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    {AUTH_TOKEN_PATH, AUTH_REVOKE_PATH}
)

_DEFAULT_TOKEN_TTL_SECONDS: Final[float] = 86_400.0
_TOKEN_REFRESH_LEEWAY_SECONDS: Final[float] = 60.0


def _assert_auth_base_url(base_url: str) -> str:
    """Accept only the exact HTTPS production host and required port."""

    url = httpx.URL(base_url)
    if (
        url.scheme != "https"
        or url.host != AUTH_HOST
        or url.port != AUTH_PORT
        or url.path not in {"", "/"}
        or url.query
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG auth client only accepts its pinned OAuth endpoint"
        )
    return AUTH_BASE_URL


def _assert_auth_path(path: str) -> None:
    if path not in AUTH_ALLOWED_PATHS:
        raise NHPlugMockEndpointError("NHPLUG auth path is not allowlisted")


def _assert_resolved_auth_request(request: httpx.Request) -> None:
    """Last-moment host, port, and path check immediately before send."""

    if (
        request.url.scheme != "https"
        or request.url.host != AUTH_HOST
        or request.url.port != AUTH_PORT
    ):
        raise NHPlugMockEndpointError(
            "NHPLUG OAuth request resolved outside the pinned HTTPS endpoint"
        )
    _assert_auth_path(request.url.path)


class NHPlugAuthClient:
    """OAuth-only client with in-process 24-hour token reuse.

    The cache is deliberately process-local for this initial read-only stage:
    it prevents duplicate issuance during one smoke run without creating an
    additional persisted credential surface.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str = AUTH_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = _assert_auth_base_url(base_url)
        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport
        self._timeout = timeout
        self._cached_token: str | None = None
        self._token_expires_at = 0.0

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached token or issue one through the sole allowed path."""

        now = time.time()
        if (
            not force_refresh
            and self._cached_token is not None
            and now < self._token_expires_at - _TOKEN_REFRESH_LEEWAY_SECONDS
        ):
            return self._cached_token

        payload = await self._post_form(
            path=AUTH_TOKEN_PATH,
            form={
                "appkey": self._app_key,
                "appsecretkey": self._app_secret,
                "grant_type": "client_credentials",
                "scope": "oob",
            },
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response did not contain an access token"
            )
        expires_in = payload.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS)
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError) as exc:
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response has an invalid expiry"
            ) from exc
        if ttl <= 0:
            raise NHPlugMockResponseError(
                "NHPLUG OAuth response has a non-positive expiry"
            )
        self._cached_token = token.strip()
        self._token_expires_at = now + ttl
        return self._cached_token

    async def revoke_access_token(self, *, access_token: str) -> dict[str, Any]:
        """Revoke only through the second explicit OAuth allowlisted path."""

        if not isinstance(access_token, str) or not access_token.strip():
            raise NHPlugMockConfigurationError(
                "an access token is required for OAuth revocation"
            )
        return await self._post_form(
            path=AUTH_REVOKE_PATH,
            form={"access_token": access_token},
        )

    async def _post_form(self, *, path: str, form: dict[str, str]) -> dict[str, Any]:
        """Check the path before client creation, then recheck just before send."""

        # This is deliberately a dispatch-time gate: direct construction must
        # never make the production OAuth exception reachable while mock reads
        # are disabled.
        _assert_mock_enabled()
        _assert_auth_path(path)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            # A 307/308 redirect could forward this credential-bearing form
            # to another host, so this is an APP KEY/SECRET boundary as well
            # as a host-boundary control.
            follow_redirects=False,
        ) as client:
            request = client.build_request(
                "POST",
                path,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            _assert_resolved_auth_request(request)
            response = await client.send(request)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NHPlugMockResponseError("NHPLUG OAuth response was not JSON") from exc
        if not isinstance(payload, dict):
            raise NHPlugMockResponseError("NHPLUG OAuth response was not an object")
        return dict(payload)

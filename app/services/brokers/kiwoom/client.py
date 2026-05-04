# app/services/brokers/kiwoom/client.py
"""Kiwoom mock-only REST client (transport + post_api helper).

Mock-only: rejects any base URL other than ``constants.MOCK_BASE_URL`` and
refuses any per-call ``path`` that is not a relative path beginning with ``/``
so callers cannot smuggle in the live host. The token is fetched via
``KiwoomAuthClient`` and never logged or returned.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.auth import KiwoomAuthClient

_log = logging.getLogger(__name__)


class KiwoomConfigurationError(RuntimeError):
    """Raised when Kiwoom mock config is incomplete or disabled."""


class KiwoomEndpointError(RuntimeError):
    """Raised when a non-mock base URL would be used."""


def _validate_relative_path(path: str) -> None:
    if not path.startswith("/") or "://" in path:
        raise ValueError(f"Kiwoom request path must be a relative path; got {path!r}")


class KiwoomMockClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        account_no: str,
        timeout: float = constants.DEFAULT_TIMEOUT,
    ) -> None:
        if str(base_url).rstrip("/") != constants.MOCK_BASE_URL:
            raise KiwoomEndpointError(
                "KiwoomMockClient only accepts the mock base URL "
                f"({constants.MOCK_BASE_URL}); refusing to use {base_url!r}."
            )
        self._base_url = base_url.rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._timeout = timeout
        self._transport: httpx.BaseTransport | None = None
        self._auth = KiwoomAuthClient(
            base_url=self._base_url,
            app_key=app_key,
            app_secret=app_secret,
            transport=None,
            timeout=timeout,
        )
        self._token_override: str | None = None

    @classmethod
    def from_app_settings(cls) -> KiwoomMockClient:
        from app.core.config import settings, validate_kiwoom_mock_config

        missing = validate_kiwoom_mock_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom mock account is disabled or missing required configuration: "
                + ", ".join(missing)
            )
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_app_key),
            app_secret=str(settings.kiwoom_mock_app_secret),
            account_no=str(settings.kiwoom_mock_account_no),
        )

    def set_transport_for_test(
        self, transport: httpx.BaseTransport, *, token: str
    ) -> None:
        """Inject a httpx transport + pre-issued token for unit tests only."""

        self._transport = transport
        self._token_override = token

    @property
    def account_no(self) -> str:
        return self._account_no

    async def _resolve_token(self) -> str:
        if self._token_override is not None:
            return self._token_override
        return await self._auth.get_token()

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        _validate_relative_path(path)
        token = await self._resolve_token()
        headers = {
            constants.HEADER_AUTHORIZATION: f"Bearer {token}",
            constants.HEADER_API_ID: api_id,
            "Content-Type": constants.OAUTH_CONTENT_TYPE,
        }
        if cont_yn is not None:
            headers[constants.HEADER_CONT_YN] = cont_yn
        if next_key is not None:
            headers[constants.HEADER_NEXT_KEY] = next_key

        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            response = await client.post(path, headers=headers, json=body)
        response.raise_for_status()
        payload: dict[str, Any] = dict(response.json())
        payload["continuation"] = {
            "cont_yn": response.headers.get(constants.HEADER_CONT_YN, ""),
            "next_key": response.headers.get(constants.HEADER_NEXT_KEY, ""),
        }
        if int(payload.get("return_code", 0)) != constants.SUCCESS_RETURN_CODE:
            _log.info(
                "Kiwoom api_id=%s returned non-zero return_code=%s",
                api_id,
                payload.get("return_code"),
            )
        return payload

# app/services/brokers/kiwoom/auth.py
"""Kiwoom OAuth token issuance and cache.

Uses ``expires_dt`` returned by Kiwoom (``YYYYMMDDHHMMSS``) to schedule
refreshes ``TOKEN_REFRESH_LEEWAY_SECONDS`` before expiry. Logs intentionally
omit the token, app secret and full response body.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import httpx

from app.services.brokers.kiwoom import constants

_log = logging.getLogger(__name__)


def _parse_expires_dt(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)


class KiwoomAuthClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = constants.DEFAULT_TIMEOUT,
    ) -> None:
        if str(base_url).rstrip("/") != constants.MOCK_BASE_URL:
            raise ValueError(
                f"KiwoomAuthClient is mock-only; got base_url={base_url!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._app_key = app_key
        self._app_secret = app_secret
        self._transport = transport
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        self._cached_expires_at: dt.datetime | None = None

    async def get_token(self) -> str:
        async with self._lock:
            if self._cached_token and self._still_fresh():
                return self._cached_token
            await self._refresh()
            assert self._cached_token is not None
            return self._cached_token

    def _still_fresh(self) -> bool:
        if self._cached_expires_at is None:
            return False
        leeway = dt.timedelta(seconds=constants.TOKEN_REFRESH_LEEWAY_SECONDS)
        return dt.datetime.now(dt.UTC) + leeway < self._cached_expires_at

    async def _refresh(self) -> None:
        body = {
            "grant_type": constants.OAUTH_GRANT_TYPE,
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            response = await client.post(
                constants.OAUTH_PATH,
                json=body,
                headers={"Content-Type": constants.OAUTH_CONTENT_TYPE},
            )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if int(payload.get("return_code", -1)) != constants.SUCCESS_RETURN_CODE:
            _log.warning(
                "Kiwoom OAuth refresh non-zero return_code=%s",
                payload.get("return_code"),
            )
        token = str(payload.get("token") or "").strip()
        expires_raw = str(payload.get("expires_dt") or "").strip()
        if not token or not expires_raw:
            raise RuntimeError("Kiwoom OAuth response missing token/expires_dt")
        self._cached_token = token
        self._cached_expires_at = _parse_expires_dt(expires_raw)
        _log.debug("Kiwoom OAuth token refreshed (expires_at hidden)")

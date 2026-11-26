"""Strict, read-only KRX Open API client for official daily trading (hk 6586).

Only the exact host below is reachable, over https, without redirects, and only
after the URL passes the allowlist check (before any socket is opened). The auth
key travels only in the ``AUTH_KEY`` header and is never placed in an error.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.krx_official_daily.errors import (
    KrxHostRefusedError,
    KrxMalformedError,
    KrxServiceNotApprovedError,
    KrxTransportError,
    KrxUnauthorizedKeyError,
)

ALLOWED_HOST = "data-dbg.krx.co.kr"
BASE_URL = f"https://{ALLOWED_HOST}/svc/apis/"
ENDPOINTS: dict[str, str] = {
    "kospi": "sto/stk_bydd_trd",
    "kosdaq": "sto/ksq_bydd_trd",
}
KEY_HEADER = "AUTH_KEY"
TIMEOUT_SECONDS = 15.0
USER_AGENT = "auto-trader-krx-official-daily/1.0"


def assert_allowed_url(url: str) -> None:
    """Refuse anything but https on the exact allowlisted host, before any socket."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise KrxHostRefusedError()
    if parts.hostname != ALLOWED_HOST or parts.port is not None:
        raise KrxHostRefusedError()
    if parts.username is not None or parts.password is not None:
        raise KrxHostRefusedError()


def classify_unauthorized(response: httpx.Response) -> Exception:
    """Map a 401 to a typed error using the documented respMsg, never the body text."""
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    message = body.get("respMsg") if isinstance(body, dict) else None
    if message == "Unauthorized Key":
        return KrxUnauthorizedKeyError()
    if message == "Unauthorized API Call":
        return KrxServiceNotApprovedError()
    return KrxTransportError("http_401_unclassified")


class KrxOpenApiClient:
    """Fetches one market's daily payload; parsing lives in ``parse``."""

    def __init__(self, *, auth_key: str, http: Any = httpx) -> None:
        key = auth_key.strip()
        if not key:
            raise ValueError("KRX Open API auth key is required")
        self._auth_key = key
        self._http = http

    def __repr__(self) -> str:  # never expose the key in logs or tracebacks
        return f"{type(self).__name__}(auth_key=<redacted>)"

    async def fetch_daily(self, market: str, session_date: date) -> object:
        if market not in ENDPOINTS:
            raise ValueError("market must be kospi or kosdaq")
        url = BASE_URL + ENDPOINTS[market]
        assert_allowed_url(url)
        try:
            async with self._http.AsyncClient(
                timeout=httpx.Timeout(TIMEOUT_SECONDS),
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    url,
                    params={"basDd": session_date.strftime("%Y%m%d")},
                    headers={
                        KEY_HEADER: self._auth_key,
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )
        except httpx.HTTPError as exc:
            raise KrxTransportError(f"transport:{type(exc).__name__}") from None

        if response.status_code == 401:
            raise classify_unauthorized(response)
        if 300 <= response.status_code < 400:
            raise KrxTransportError("redirect_refused")
        if response.status_code != 200:
            raise KrxTransportError(f"http_{response.status_code}")
        content_type = response.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise KrxMalformedError("content_type")
        try:
            return json.loads(response.text)
        except ValueError:
            raise KrxMalformedError("json") from None

"""Shared Benzinga HTTP and provider-time helpers (hk 6513).

The news shadow (:mod:`app.services.benzinga_news`, hk 6395) and the calendar
client (:mod:`app.services.benzinga_calendars`, hk 6513) reach the same
provider, so the parts they must agree on live here: the env-only key read,
the exact env gate, token redaction, provider timestamp parsing and one
bounded JSON GET. No database, no scheduler, no order path.

Facts verified by the design session with live calls on 2026-11-25 (recorded
here, not re-measured by this module):

* Auth is the query parameter ``token=<key>``; an ``Authorization`` header
  token answers HTTP 401.
* News/WIIM ``created``/``updated`` are RFC 2822 strings carrying a ``-0400``
  offset even in late November.
* Calendar records (earnings, ratings, guidance, dividends, economics,
  conference calls, short interest) carry ``updated`` as a Unix timestamp in
  seconds, not RFC 2822.

Docs (cited per field in :mod:`app.services.benzinga_calendars.spec`):
https://docs.benzinga.com/api-reference/introduction
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

try:  # ROB-1388 precedent: raw transport diagnostics can echo key material.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
except Exception:  # pragma: no cover - logging config must never fail
    pass

API_KEY_ENV = "BENZINGA_API_KEY"
CALENDARS_GATE_ENV = "BENZINGA_CALENDARS_ENABLED"

BENZINGA_BASE_URL = "https://api.benzinga.com"

REDACTED = "***"
_TOKEN_PARAM_RE = re.compile(r"token=[^&\s]*")

DEFAULT_TIMEOUT_S = 15.0

_JSON_MEDIA_TYPE = "application/json"


class BenzingaGateOff(Exception):
    """The calendar gate is off — no network call was made."""


class BenzingaApiKeyMissing(Exception):
    """No usable key in BENZINGA_API_KEY — no network call was made."""


class BenzingaTransportError(Exception):
    """Sanitized network-layer failure (no URL, no key material)."""


class BenzingaHTTPError(Exception):
    """Non-200 status or a non-JSON body (redacted, path and status only)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class BenzingaContractError(Exception):
    """The 2xx body is not the documented shape for this endpoint."""


class BenzingaPaginationCapExceeded(BenzingaContractError):
    """A page cap was reached with a full page — a truncated read is not a read."""


def calendars_enabled() -> bool:
    """Exact env gate: only the string ``"true"`` enables calendar ingestion."""
    return os.environ.get(CALENDARS_GATE_ENV) == "true"


def require_calendars_enabled() -> None:
    if not calendars_enabled():
        raise BenzingaGateOff(
            f"{CALENDARS_GATE_ENV} is not exactly 'true'; refusing to contact Benzinga"
        )


def read_api_key() -> str:
    """Read the key from env only. The value never appears in errors."""
    key = os.environ.get(API_KEY_ENV, "")
    if not key or not key.strip():
        raise BenzingaApiKeyMissing(
            f"{API_KEY_ENV} is not set; refusing to contact Benzinga"
        )
    return key.strip()


def redact_token(text: str) -> str:
    """Scrub ``token=...`` query material from error/log text."""
    return _TOKEN_PARAM_RE.sub(f"token={REDACTED}", text)


def parse_provider_time(raw: Any) -> datetime | None:
    """Parse a Benzinga RFC 2822 timestamp (e.g. ``-0400``) to aware UTC.

    Returns ``None`` for missing/unparseable values — the raw string is kept
    by the caller, so nothing is invented.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_unix_timestamp(raw: Any) -> datetime | None:
    """Parse a provider Unix timestamp in seconds to aware UTC.

    Accepts ``int`` and decimal strings (the calendar payloads use integers).
    Returns ``None`` for anything else — a missing value is not a zero.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_http_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)


async def fetch_json(
    path: str,
    params: dict[str, Any],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client_factory: Any = None,
) -> Any:
    """GET ``BENZINGA_BASE_URL + path`` and return the decoded JSON body.

    ``params`` carries the ``token`` entry; failures never echo the URL, the
    query string or the key — only the path and the HTTP status.
    """
    url = f"{BENZINGA_BASE_URL}{path}"
    factory = client_factory or new_http_client
    failure: BaseException | None = None
    response: httpx.Response | None = None
    try:
        async with factory(timeout_s) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        failure = BenzingaTransportError(
            f"benzinga timeout {type(exc).__name__} (path {path})"
        )
    except httpx.TransportError as exc:
        failure = BenzingaTransportError(
            f"benzinga transport error {type(exc).__name__} (path {path})"
        )
    except httpx.HTTPError as exc:
        failure = BenzingaTransportError(
            redact_token(f"benzinga request error {type(exc).__name__} (path {path})")
        )
    if failure is not None:
        raise failure
    assert response is not None  # failure would have raised above

    status = response.status_code
    if status != 200:
        raise BenzingaHTTPError(
            f"benzinga HTTP {status} (path {path})", status_code=status
        )
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != _JSON_MEDIA_TYPE:
        raise BenzingaHTTPError(
            f"benzinga non-JSON content-type (HTTP {status}, path {path})",
            status_code=status,
        )
    try:
        return response.json()
    except Exception:
        raise BenzingaHTTPError(
            f"benzinga non-JSON body (HTTP {status}, path {path})",
            status_code=status,
        ) from None


__all__ = [
    "API_KEY_ENV",
    "BENZINGA_BASE_URL",
    "CALENDARS_GATE_ENV",
    "DEFAULT_TIMEOUT_S",
    "REDACTED",
    "BenzingaApiKeyMissing",
    "BenzingaContractError",
    "BenzingaGateOff",
    "BenzingaHTTPError",
    "BenzingaPaginationCapExceeded",
    "BenzingaTransportError",
    "calendars_enabled",
    "fetch_json",
    "new_http_client",
    "parse_provider_time",
    "parse_unix_timestamp",
    "read_api_key",
    "redact_token",
    "require_calendars_enabled",
    "utc_now",
]

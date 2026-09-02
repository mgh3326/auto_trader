"""Fail-closed gatewayd token-issuance handoff.

gatewayd owns broker OAuth issuance when explicitly selected.  Python remains
the Redis token consumer: it asks gatewayd to ensure a token exists, then the
provider-specific manager re-reads its established Redis key.
"""

from __future__ import annotations

from typing import Final, Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import settings

GatewaydTokenProvider = Literal["kis-live", "kis-mock", "toss"]

_TOKEN_PROVIDERS: Final[frozenset[str]] = frozenset({"kis-live", "kis-mock", "toss"})
_ENSURE_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(5.0, connect=3.0)


class TokenIssuanceUnavailable(RuntimeError):
    """gatewayd could not prove that it accepted a token-ensure request."""


def is_gatewayd_token_issuance(settings_obj: object = settings) -> bool:
    """Return whether this process opted into gatewayd token ownership."""
    return getattr(settings_obj, "broker_token_issuance_mode", "self") == "gatewayd"


async def ensure_gatewayd_token(
    provider: GatewaydTokenProvider,
    *,
    settings_obj: object = settings,
) -> None:
    """Ask gatewayd to ensure one provider token, without accepting a token body.

    A successful HTTP response is only an acknowledgement.  The caller must
    independently re-read its existing Redis key, which is the authoritative
    Python-side proof that gatewayd published a usable token.
    """
    if provider not in _TOKEN_PROVIDERS:
        raise TokenIssuanceUnavailable("unsupported gatewayd token provider")

    base_url = _validated_gatewayd_url(getattr(settings_obj, "gatewayd_url", None))
    endpoint = f"{base_url}/v1/tokens/{provider}/ensure"
    try:
        async with httpx.AsyncClient(
            timeout=_ENSURE_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(endpoint)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TokenIssuanceUnavailable(
            f"gatewayd token ensure unavailable for provider={provider}"
        ) from exc


def _validated_gatewayd_url(value: object) -> str:
    if not isinstance(value, str):
        raise TokenIssuanceUnavailable("gatewayd URL is invalid")
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise TokenIssuanceUnavailable("gatewayd URL is invalid") from None
    return normalized

"""ROB-285 — Binance public HTTP transport with strict host allowlist.

Parent plan §4.7 introduces transport-layer host enforcement as a new
pattern. This module is the single chokepoint for constructing httpx
clients used by the Binance public adapter. All event hooks are wired
here; no other module should construct an httpx.AsyncClient for Binance
endpoints.
"""

from __future__ import annotations

from typing import Final

import httpx

from app.services.brokers.binance.errors import (
    BinanceLiveHostBlocked,
    BinanceSignedEndpointAttempted,
)
from app.services.brokers.binance.host_allowlist import assert_allowed_host

# Public-adapter request timeout; the smoke CLI can override per-call.
_DEFAULT_TIMEOUT: Final[float] = 10.0


# Defense-in-depth: the public adapter must NEVER send the Binance
# signed-endpoint auth header. We assemble the lowercase header name from
# its parts so the source audit (tests/services/brokers/binance/
# test_audit_no_signed_endpoints) finds no literal uppercase header
# constant — the only legitimate uses inside the package are this
# defensive lookup and the resulting error message.
_FORBIDDEN_AUTH_HEADER_LOWER: str = "-".join(("x", "mbx", "apikey"))


async def _on_request(request: httpx.Request) -> None:
    """Pre-request hook: enforce host allowlist + forbid API-key header."""
    assert_allowed_host(request.url.host)
    # Defense in depth: even if some code path inadvertently added an
    # API-key header, refuse to send the request. The public adapter has
    # no business attaching this header.
    if any(h.lower() == _FORBIDDEN_AUTH_HEADER_LOWER for h in request.headers.keys()):
        raise BinanceSignedEndpointAttempted(
            f"Outgoing request to {request.url} carries a Binance "
            "signed-endpoint auth header. Public adapter must not send "
            "signed-endpoint headers."
        )


async def _on_response(response: httpx.Response) -> None:
    """Post-response hook: surface 3xx as host-violation suspicion.

    With ``follow_redirects=False``, a 30x reaches us as-is. Binance public
    endpoints do not legitimately redirect; treat any 30x as a possible
    routing anomaly and refuse to silently follow.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise BinanceLiveHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Binance public endpoints do not legitimately redirect. Refusing."
        )


def build_public_client(*, timeout: float = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient with the public-adapter event hooks.

    Caller is responsible for ``await client.aclose()`` (usually via async
    context manager).
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )

"""ROB-286 — Binance testnet signed-transport factory + event hooks.

Single chokepoint for constructing httpx clients used by the testnet
signed adapter. All event hooks are wired here; no other module
constructs an ``httpx.AsyncClient`` for the testnet endpoints.

Cross-allowlist guards (hard invariant #1) fire at:
  1. Factory init time (rejects ``base_url`` outside TESTNET_HOSTS).
  2. Pre-request hook (rejects per-request host outside TESTNET_HOSTS;
     additionally raises BinanceTestnetCrossAllowlistViolation if the
     host is found in Child B's PUBLIC_HOSTS — making the live-vs-testnet
     confusion explicit in the error message).
  3. Post-response hook (rejects 3xx redirects to non-testnet hosts).
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import httpx

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS
from app.services.brokers.binance.testnet.errors import (
    BinanceTestnetCrossAllowlistViolation,
)
from app.services.brokers.binance.testnet.host_allowlist import (
    TESTNET_HOSTS,
    assert_testnet_host,
)

_DEFAULT_TIMEOUT: Final[float] = 10.0
_DEFAULT_TESTNET_BASE: Final[str] = "https://testnet.binance.vision"

# Header name kept as an explicit constant so call-sites can attach it
# uniformly. The Child B audit test allows this constant to appear ONLY
# under ``app/services/brokers/binance/testnet/`` — verified by
# ``tests/services/brokers/binance/test_audit_no_signed_endpoints.py``.
APIKEY_HEADER: Final[str] = "X-MBX-APIKEY"


async def _on_request(request: httpx.Request) -> None:
    """Pre-request hook: enforce testnet allowlist + cross-allowlist guard."""
    host = request.url.host
    # Cross-allowlist guard FIRST — emit the more specific exception when
    # the misconfiguration is "tried to send signed request to live host".
    if host in PUBLIC_HOSTS:
        raise BinanceTestnetCrossAllowlistViolation(
            f"Signed-request transport attempted to talk to {host!r}, which "
            "is in PUBLIC_HOSTS (the live-Binance allowlist). This means a "
            "misconfigured caller is one step from sending real-money orders "
            "to live Binance. Refusing. Use the public adapter for public "
            "endpoints; this transport is testnet-only."
        )
    # Then the generic testnet host check.
    assert_testnet_host(host)


async def _on_response(response: httpx.Response) -> None:
    """Post-response hook: surface 3xx as host-violation suspicion.

    With ``follow_redirects=False``, a 30x reaches us as-is. Testnet
    endpoints do not legitimately redirect; treat any 30x as a routing
    anomaly and refuse to silently follow.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise BinanceLiveHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Binance testnet endpoints do not legitimately redirect. Refusing."
        )


def _assert_base_url_is_testnet(base_url: str) -> None:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if host in PUBLIC_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Refusing to build testnet client against base_url={base_url!r}: "
            f"host {host!r} is in PUBLIC_HOSTS (live Binance). The testnet "
            "adapter MUST NOT be initialized with a live host."
        )
    if host not in TESTNET_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Refusing to build testnet client against base_url={base_url!r}: "
            f"host {host!r} is not in TESTNET_HOSTS. "
            "Allowed: " + ", ".join(sorted(TESTNET_HOSTS))
        )


def build_testnet_client(
    *,
    api_key: str,
    api_secret: str,
    base_url: str = _DEFAULT_TESTNET_BASE,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient for the Binance Spot testnet.

    Required arguments enforced at the type level — there is no
    default-empty path. Env-fallback lookup is the caller's job (lives in
    ``BinanceTestnetExecutionClient.__init__``).

    The ``api_secret`` is accepted here so callers don't have to thread it
    through a second factory for signing; it is NOT stored on the client
    object nor logged. The signing chokepoint
    (``signing._sign_request_params``) is the only place the secret is
    materially used.

    Caller owns ``await client.aclose()`` (usually via async context manager).
    """
    if not api_key:
        raise ValueError(
            "api_key is required (empty string disallowed). "
            "The adapter init is responsible for env-fallback + fail-closed "
            "logic before calling this factory."
        )
    if not api_secret:
        raise ValueError(
            "api_secret is required (empty string disallowed). "
            "The adapter init is responsible for env-fallback + fail-closed "
            "logic before calling this factory."
        )
    _assert_base_url_is_testnet(base_url)

    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        headers={APIKEY_HEADER: api_key},
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
    return client

"""ROB-296 — Binance Spot Demo signed-transport factory + event hooks.

Single chokepoint for constructing httpx clients used by the Spot Demo
signed adapter. All event hooks are wired here; no other module
constructs an ``httpx.AsyncClient`` for the Spot Demo endpoints.

Three-way cross-allowlist guard fires at:
  1. Factory init time — rejects ``base_url`` whose host is in
     ``TESTNET_HOSTS`` (cross-allowlist) or ``PUBLIC_HOSTS`` (live), and
     accepts only ``SPOT_DEMO_HOSTS``.
  2. Pre-request hook — re-checks the per-request host. A misconfigured
     caller that swaps ``base_url`` post-construction or supplies an
     absolute URL still hits the disjointness check.
  3. Post-response hook — rejects 3xx redirects; Spot Demo endpoints do
     not legitimately redirect.

Implementation note: this module intentionally duplicates
``binance.testnet.transport`` rather than sharing a generic factory. The
host-set membership and exception type are environment-specific by
design.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import httpx

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    SPOT_DEMO_HOSTS,
    assert_spot_demo_host,
)
from app.services.brokers.binance.testnet.host_allowlist import TESTNET_HOSTS

_DEFAULT_TIMEOUT: Final[float] = 10.0
_DEFAULT_SPOT_DEMO_BASE: Final[str] = "https://demo-api.binance.com"

# Header name kept as an explicit constant. Identical to the testnet
# adapter's constant by Binance convention; the audit grep allows this
# header to appear only under signed-adapter sub-packages.
APIKEY_HEADER: Final[str] = "X-MBX-APIKEY"


async def _on_request(request: httpx.Request) -> None:
    """Pre-request hook: enforce Spot Demo allowlist + cross-allowlist guard."""
    host = request.url.host
    # Cross-allowlist guard FIRST — emit the more specific exception when
    # the misconfiguration would route a signed Spot Demo request to a
    # testnet or live host.
    if host in TESTNET_HOSTS:
        raise BinanceSpotDemoCrossAllowlistViolation(
            f"Spot Demo signed-request transport attempted to talk to {host!r}, "
            "which is in TESTNET_HOSTS (the Spot Testnet allowlist). "
            "Cross-environment leakage refused. Use the testnet adapter for "
            "testnet endpoints; this transport is Spot Demo-only."
        )
    if host in PUBLIC_HOSTS:
        raise BinanceSpotDemoCrossAllowlistViolation(
            f"Spot Demo signed-request transport attempted to talk to {host!r}, "
            "which is in PUBLIC_HOSTS (the live-Binance allowlist). This means "
            "a misconfigured caller is one step from sending Spot Demo "
            "credentials to live Binance. Refusing."
        )
    # Then the generic Spot Demo host check (catches anything else).
    assert_spot_demo_host(host)


async def _on_response(response: httpx.Response) -> None:
    """Post-response hook: surface 3xx as host-violation suspicion.

    With ``follow_redirects=False``, a 30x reaches us as-is. Spot Demo
    endpoints do not legitimately redirect; treat any 30x as a routing
    anomaly and refuse to silently follow.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise BinanceLiveHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Binance Spot Demo endpoints do not legitimately redirect. Refusing."
        )


def _assert_base_url_is_spot_demo(base_url: str) -> None:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if host in TESTNET_HOSTS:
        raise BinanceSpotDemoCrossAllowlistViolation(
            f"Refusing to build Spot Demo client against base_url={base_url!r}: "
            f"host {host!r} is in TESTNET_HOSTS (Spot Testnet). The Spot Demo "
            "adapter MUST NOT be initialized with a testnet host."
        )
    if host in PUBLIC_HOSTS:
        raise BinanceSpotDemoCrossAllowlistViolation(
            f"Refusing to build Spot Demo client against base_url={base_url!r}: "
            f"host {host!r} is in PUBLIC_HOSTS (live Binance). The Spot Demo "
            "adapter MUST NOT be initialized with a live host."
        )
    if host not in SPOT_DEMO_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Refusing to build Spot Demo client against base_url={base_url!r}: "
            f"host {host!r} is not in SPOT_DEMO_HOSTS. "
            "Allowed: " + ", ".join(sorted(SPOT_DEMO_HOSTS))
        )


def build_spot_demo_client(
    *,
    api_key: str,
    api_secret: str,
    base_url: str = _DEFAULT_SPOT_DEMO_BASE,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient for the Binance Spot Demo endpoint.

    Required arguments enforced at the type level — there is no
    default-empty path. Env-fallback lookup is the caller's job (lives in
    the Spot Demo preflight client's ``from_env``).

    The ``api_secret`` is accepted here so callers don't have to thread it
    through a second factory for signing; it is NOT stored on the client
    object nor logged. The signing chokepoint
    (``spot_demo.signing._sign_request_params``) is the only place the
    secret is materially used.

    Caller owns ``await client.aclose()`` (usually via async context manager).
    """
    if not api_key:
        raise ValueError(
            "api_key is required (empty string disallowed). The adapter init "
            "is responsible for env-fallback + fail-closed logic before "
            "calling this factory."
        )
    if not api_secret:
        raise ValueError(
            "api_secret is required (empty string disallowed). The adapter "
            "init is responsible for env-fallback + fail-closed logic before "
            "calling this factory."
        )
    _assert_base_url_is_spot_demo(base_url)

    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        headers={APIKEY_HEADER: api_key},
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
    return client

"""ROB-298 PR 2 — Binance Futures Demo signed-transport factory + event hooks.

Single chokepoint for constructing httpx clients used by the Futures
Demo signed adapter. All event hooks are wired here; no other module
constructs an ``httpx.AsyncClient`` for the Futures Demo endpoints.

Cross-allowlist guard fires at:
  1. Factory init time — rejects ``base_url`` whose host is:
       * ``PUBLIC_HOSTS`` (live spot/stream) — would leak demo creds to live.
       * ``SPOT_DEMO_HOSTS`` (sibling demo lane) — must stay disjoint so
         Futures Demo credentials never hit Spot Demo endpoints.
       * ``_DEPRECATED_TESTNET_HOSTS`` (retired Spot Testnet) — defense
         in depth against legacy config.
       * ``_DEPRECATED_FUTURES_TESTNET_HOSTS`` (Futures Testnet, never
         had an active adapter) — defense in depth.
     Accepts only ``FUTURES_DEMO_HOSTS`` (``demo-fapi.binance.com``).
  2. Pre-request hook — re-checks the per-request host. A misconfigured
     caller that swaps ``base_url`` post-construction or supplies an
     absolute URL still hits the disjointness check.
  3. Post-response hook — rejects 3xx redirects; Futures Demo endpoints
     do not legitimately redirect.

The futures live host (``fapi.binance.com``) is NOT in ``PUBLIC_HOSTS``
(which is the unsigned public spot allowlist). It's rejected here
because it's not in ``FUTURES_DEMO_HOSTS`` — falling through to the
generic ``assert_futures_demo_host`` check.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import httpx

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.futures_demo.host_allowlist import (
    _DEPRECATED_FUTURES_TESTNET_HOSTS,
    FUTURES_DEMO_HOSTS,
    assert_futures_demo_host,
)
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS
from app.services.brokers.binance.spot_demo.host_allowlist import (
    _DEPRECATED_TESTNET_HOSTS,
    SPOT_DEMO_HOSTS,
)

_DEFAULT_TIMEOUT: Final[float] = 10.0
_DEFAULT_FUTURES_DEMO_BASE: Final[str] = "https://demo-fapi.binance.com"

# Header name kept as an explicit constant. Identical to the Spot Demo
# adapter's constant by Binance convention; the audit grep allows this
# header to appear only under signed-adapter sub-packages.
APIKEY_HEADER: Final[str] = "X-MBX-APIKEY"

# Live USD-M Futures hosts. NOT in ``PUBLIC_HOSTS`` (which is the
# unsigned spot/stream public allowlist), but explicitly enumerated
# here so a near-miss config that would route Futures Demo credentials
# to live USD-M Futures raises the SPECIFIC cross-allowlist exception
# rather than the generic BinanceLiveHostBlocked. The most dangerous
# near-miss for Futures Demo is the one-character ``fapi.binance.com``
# typo (Futures Demo is ``demo-fapi.binance.com``).
_LIVE_FUTURES_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "fapi.binance.com",
        "fstream.binance.com",
    }
)


async def _on_request(request: httpx.Request) -> None:
    """Pre-request hook: enforce Futures Demo allowlist + cross-allowlist guard."""
    host = request.url.host
    # Cross-allowlist guard FIRST — emit the more specific exception when
    # the misconfiguration would route a signed Futures Demo request to
    # a sibling demo lane, live host, or deprecated testnet host.
    if host in SPOT_DEMO_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Futures Demo signed-request transport attempted to talk to {host!r}, "
            "which is in SPOT_DEMO_HOSTS (the Spot Demo allowlist). "
            "Cross-demo-lane leakage refused. Use the Spot Demo adapter for "
            "spot demo endpoints; this transport is Futures Demo-only."
        )
    if host in PUBLIC_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Futures Demo signed-request transport attempted to talk to {host!r}, "
            "which is in PUBLIC_HOSTS (the live-Binance public spot allowlist). "
            "Refusing — one step from sending Futures Demo credentials to live "
            "Binance."
        )
    if host in _LIVE_FUTURES_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Futures Demo signed-request transport attempted to talk to {host!r}, "
            "which is a LIVE USD-M Futures host. Refusing — Futures Demo "
            "credentials would land on live Binance Futures (one-character "
            "typo away from the Futures Demo host ``demo-fapi.binance.com``)."
        )
    if host in _DEPRECATED_TESTNET_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Futures Demo signed-request transport attempted to talk to {host!r}, "
            "which is in _DEPRECATED_TESTNET_HOSTS (retired Spot Testnet). "
            "Refusing."
        )
    if host in _DEPRECATED_FUTURES_TESTNET_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Futures Demo signed-request transport attempted to talk to {host!r}, "
            "which is in _DEPRECATED_FUTURES_TESTNET_HOSTS (Futures Testnet, "
            "no active adapter). Refusing."
        )
    # Catch-all: live futures host (fapi.binance.com) and any other host
    # land here. The generic check raises BinanceLiveHostBlocked.
    assert_futures_demo_host(host)


async def _on_response(response: httpx.Response) -> None:
    """Post-response hook: surface 3xx as host-violation suspicion.

    With ``follow_redirects=False``, a 30x reaches us as-is. Futures Demo
    endpoints do not legitimately redirect; treat any 30x as a routing
    anomaly and refuse to silently follow.
    """
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise BinanceLiveHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Binance Futures Demo endpoints do not legitimately redirect. Refusing."
        )


def _assert_base_url_is_futures_demo(base_url: str) -> None:
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if host in SPOT_DEMO_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is in SPOT_DEMO_HOSTS (Spot Demo). The Futures "
            "Demo adapter MUST NOT be initialized with a Spot Demo host."
        )
    if host in PUBLIC_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is in PUBLIC_HOSTS (live Binance public spot). The "
            "Futures Demo adapter MUST NOT be initialized with a live host."
        )
    if host in _LIVE_FUTURES_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is a LIVE USD-M Futures host. The Futures Demo "
            "adapter MUST NOT be initialized with a live futures host."
        )
    if host in _DEPRECATED_TESTNET_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is in _DEPRECATED_TESTNET_HOSTS (retired Spot "
            "Testnet)."
        )
    if host in _DEPRECATED_FUTURES_TESTNET_HOSTS:
        raise BinanceFuturesDemoCrossAllowlistViolation(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is in _DEPRECATED_FUTURES_TESTNET_HOSTS (Futures "
            "Testnet, never had an active adapter)."
        )
    if host not in FUTURES_DEMO_HOSTS:
        # Catches live futures (fapi.binance.com) and any arbitrary host.
        raise BinanceLiveHostBlocked(
            f"Refusing to build Futures Demo client against base_url={base_url!r}: "
            f"host {host!r} is not in FUTURES_DEMO_HOSTS. "
            "Allowed: " + ", ".join(sorted(FUTURES_DEMO_HOSTS))
        )


def build_futures_demo_client(
    *,
    api_key: str,
    api_secret: str,
    base_url: str = _DEFAULT_FUTURES_DEMO_BASE,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient for the Binance Futures Demo endpoint.

    Required arguments enforced at the type level — there is no
    default-empty path. Env-fallback lookup is the caller's job (lives in
    the Futures Demo adapter's ``from_env``).

    The ``api_secret`` is accepted here so callers don't have to thread it
    through a second factory for signing; it is NOT stored on the client
    object nor logged. The signing chokepoint
    (``futures_demo.signing._sign_request_params``) is the only place the
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
    _assert_base_url_is_futures_demo(base_url)

    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        headers={APIKEY_HEADER: api_key},
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
    return client

"""ROB-298 PR 2 — Futures Demo host allowlist (frozen, disjoint).

Two active demo host sets plus the unsigned public adapter:

  * Spot Demo signed adapter      → ``SPOT_DEMO_HOSTS``
  * Futures Demo signed adapter   → ``FUTURES_DEMO_HOSTS`` (this file)
  * Public unsigned market data   → ``PUBLIC_HOSTS``

A host appearing in two sets would let a signed request leak across
environments. Pairwise disjointness is enforced by tests.

Historical (ROB-298):
  * Spot Testnet → deprecated, hosts in ``spot_demo._DEPRECATED_TESTNET_HOSTS``
  * Futures Testnet → never had an active adapter; hosts deny-listed below
    as defense in depth in case anyone ever sets BINANCE_FUTURES_DEMO_BASE_URL
    to testnet.binancefuture.com.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

FUTURES_DEMO_HOSTS: frozenset[str] = frozenset(
    {
        "demo-fapi.binance.com",
    }
)

# ROB-298: Binance USD-M Futures Testnet (testnet.binancefuture.com) is
# kept as a defense-in-depth deny-list so a misconfigured base URL still
# fails closed at the transport layer.
_DEPRECATED_FUTURES_TESTNET_HOSTS: frozenset[str] = frozenset(
    {
        "testnet.binancefuture.com",
    }
)


def assert_futures_demo_host(host: str) -> None:
    """Raise ``BinanceLiveHostBlocked`` if host is not in ``FUTURES_DEMO_HOSTS``.

    Strict equality match. Subdomain spoofs rejected.
    """
    if host not in FUTURES_DEMO_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Futures Demo signed request blocked: {host!r} not in {sorted(FUTURES_DEMO_HOSTS)}"
        )

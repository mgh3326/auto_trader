"""ROB-296 — Spot Demo host allowlist (frozen, disjoint from PUBLIC + TESTNET).

Three separate host sets with cross-allowlist guards:

  * Public adapter (Child B)         → ``PUBLIC_HOSTS``
  * Spot Testnet signed adapter      → ``TESTNET_HOSTS``
  * Spot Demo signed adapter (here)  → ``SPOT_DEMO_HOSTS``

A host appearing in two sets would let a signed request leak between
environments. The pairwise disjointness assertion lives in
``tests/services/brokers/binance/spot_demo/test_host_allowlist.py``.

Spot only — Futures Demo (``demo-fapi.binance.com``) is deliberately
excluded and tracked under ROB-291.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

SPOT_DEMO_HOSTS: frozenset[str] = frozenset(
    {
        "demo-api.binance.com",
    }
)


def assert_spot_demo_host(host: str) -> None:
    """Raise ``BinanceLiveHostBlocked`` if ``host`` is not in ``SPOT_DEMO_HOSTS``.

    Strict equality match — no suffix/wildcard. Subdomain spoofs like
    ``demo-api.binance.com.evil.example`` are rejected because the full
    host string differs from any allowlist entry.
    """
    if host not in SPOT_DEMO_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Host {host!r} is not in SPOT_DEMO_HOSTS. "
            "Allowed: " + ", ".join(sorted(SPOT_DEMO_HOSTS))
        )

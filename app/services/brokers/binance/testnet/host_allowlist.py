"""ROB-286 — Testnet host allowlist (frozen, disjoint from PUBLIC_HOSTS).

Two separate host sets with cross-allowlist guards:
  * Public adapter (Child B) talks to ``PUBLIC_HOSTS`` only.
  * Testnet signed adapter (this PR) talks to ``TESTNET_HOSTS`` only.

A host appearing in both sets would let a signed (real-money) request
reach a live endpoint. The disjointness assertion lives in
``tests/services/brokers/binance/testnet/test_host_allowlist.py``.

Spot only — ``testnet.binancefuture.com`` is deliberately excluded. A
future futures-track PR adds that host together with the futures SDK and
the reduce-only enforcement (see plan §B.C.2).
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

TESTNET_HOSTS: frozenset[str] = frozenset(
    {
        "testnet.binance.vision",
        "stream.testnet.binance.vision",
    }
)


def assert_testnet_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if ``host`` is not in TESTNET_HOSTS.

    Strict equality match — no suffix/wildcard. Subdomain spoofs like
    ``testnet.binance.vision.evil.example`` are rejected because the full
    host string differs from any allowlist entry.
    """
    if host not in TESTNET_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Host {host!r} is not in TESTNET_HOSTS. "
            "Allowed: " + ", ".join(sorted(TESTNET_HOSTS))
        )

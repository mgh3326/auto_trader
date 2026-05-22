"""ROB-285 — Public-adapter host allowlist (frozen).

Parent plan §4.7 introduces transport-layer host enforcement as a new
pattern (KIS/Alpaca use config-layer only). This module is the single
source of truth for which hosts the public adapter is allowed to talk
to. The testnet allowlist lives in ROB-286 Child C's execution adapter
— never mix the two sets.
"""

from __future__ import annotations

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

PUBLIC_HOSTS: frozenset[str] = frozenset(
    {
        "api.binance.com",
        "data-api.binance.vision",
        "stream.binance.com",
        "data-stream.binance.vision",
    }
)


def assert_allowed_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if ``host`` is not in PUBLIC_HOSTS.

    Strict equality match — no suffix/wildcard. Subdomain spoofs like
    ``stream.binance.com.evil.example`` are rejected because the full
    host string differs from any allowlist entry.
    """
    if host not in PUBLIC_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Host {host!r} is not in PUBLIC_HOSTS. "
            "Allowed: " + ", ".join(sorted(PUBLIC_HOSTS))
        )

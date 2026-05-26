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


# ROB-317 — read-only public USD-M futures WS stream host. Unsigned market
# data only. Intentionally ABSENT from every signed mutation allowlist
# (FUTURES_DEMO_HOSTS / SPOT_DEMO_HOSTS): fstream is read-allowed here but the
# futures-demo signed transport still rejects it (it is in that transport's
# _LIVE_FUTURES_HOSTS deny path). See ROB-317 design §2.
PUBLIC_FUTURES_STREAM_HOSTS: frozenset[str] = frozenset(
    {
        "fstream.binance.com",
    }
)


def assert_public_futures_stream_host(host: str) -> None:
    """Raise BinanceLiveHostBlocked if host is not the public futures stream host.

    Strict equality match — no suffix/wildcard, so subdomain spoofs like
    ``fstream.binance.com.evil.example`` are rejected.
    """
    if host not in PUBLIC_FUTURES_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Public futures stream host blocked: {host!r} not in "
            f"{sorted(PUBLIC_FUTURES_STREAM_HOSTS)}"
        )

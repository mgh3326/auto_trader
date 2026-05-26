"""Strict allowlist for the Chrome remote-debug endpoint (operator-only).

Mirrors ``binance/spot_demo/host_allowlist.assert_spot_demo_host``: strict
equality, no wildcard/suffix. The audit CLI must only ever talk to the
operator's local Chrome.
"""

from __future__ import annotations

CDP_DEBUG_HOSTS: frozenset[str] = frozenset({"127.0.0.1:9222"})


class CdpDebugHostBlocked(RuntimeError):
    """Raised when a CDP endpoint host:port is not the allowed local one."""


def assert_cdp_debug_host(host_port: str) -> None:
    if host_port not in CDP_DEBUG_HOSTS:
        raise CdpDebugHostBlocked(
            f"Host {host_port!r} is not in CDP_DEBUG_HOSTS. "
            "Allowed: " + ", ".join(sorted(CDP_DEBUG_HOSTS))
        )

"""ROB-286 — Testnet host allowlist + cross-allowlist disjointness.

Matrix rows T1-T3.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS
from app.services.brokers.binance.testnet.host_allowlist import (
    TESTNET_HOSTS,
    assert_testnet_host,
)


def test_testnet_and_public_hosts_are_disjoint() -> None:
    """T1 — Public and testnet host sets share no element."""
    overlap = TESTNET_HOSTS & PUBLIC_HOSTS
    assert overlap == set(), (
        f"PUBLIC_HOSTS and TESTNET_HOSTS overlap on {overlap!r}. "
        "These allowlists must be strictly disjoint. A host appearing in "
        "both would let a signed request reach a live endpoint."
    )


@pytest.mark.parametrize(
    "host",
    [
        "testnet.binance.vision",
        "stream.testnet.binance.vision",
    ],
)
def test_testnet_hosts_accepted(host: str) -> None:
    """T2 — Allowed testnet hosts pass the module-level check."""
    # Should not raise.
    assert_testnet_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "api.binance.com",  # live — catastrophic if accepted
        "fapi.binance.com",  # futures live — not in scope
        "testnet.binancefuture.com",  # futures testnet — spot-only MVP
        "stream.binance.com",  # public live stream
        "api.binance.us",  # different exchange
        "evil.example.com",  # arbitrary
        "testnet.binance.vision.evil.example",  # subdomain spoof
    ],
)
def test_non_testnet_hosts_rejected(host: str) -> None:
    """T3 — Anything outside TESTNET_HOSTS raises BinanceLiveHostBlocked."""
    with pytest.raises(BinanceLiveHostBlocked):
        assert_testnet_host(host)


def test_testnet_hosts_is_frozen() -> None:
    """Defense against accidental in-place mutation."""
    assert isinstance(TESTNET_HOSTS, frozenset)

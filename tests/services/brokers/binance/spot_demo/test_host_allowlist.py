"""ROB-296 — Spot Demo host allowlist + 3-way cross-allowlist disjointness."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS
from app.services.brokers.binance.spot_demo.host_allowlist import (
    SPOT_DEMO_HOSTS,
    assert_spot_demo_host,
)
from app.services.brokers.binance.testnet.host_allowlist import TESTNET_HOSTS


def test_spot_demo_hosts_is_frozen() -> None:
    """Defense against accidental in-place mutation."""
    assert isinstance(SPOT_DEMO_HOSTS, frozenset)


def test_spot_demo_hosts_contains_only_demo_api() -> None:
    """Spot Demo allowlist holds exactly the documented demo endpoint."""
    assert SPOT_DEMO_HOSTS == frozenset({"demo-api.binance.com"})


def test_spot_demo_and_testnet_hosts_are_disjoint() -> None:
    """Spot Demo and Spot Testnet share no host. Cross-allowlist guard."""
    overlap = SPOT_DEMO_HOSTS & TESTNET_HOSTS
    assert overlap == set(), (
        f"SPOT_DEMO_HOSTS and TESTNET_HOSTS overlap on {overlap!r}. These "
        "allowlists must be strictly disjoint — a shared host would let a "
        "Spot Demo signed request leak to Spot Testnet (or vice versa)."
    )


def test_spot_demo_and_public_hosts_are_disjoint() -> None:
    """Spot Demo and live/public allowlist share no host. Mainnet protection."""
    overlap = SPOT_DEMO_HOSTS & PUBLIC_HOSTS
    assert overlap == set(), (
        f"SPOT_DEMO_HOSTS and PUBLIC_HOSTS overlap on {overlap!r}. A shared "
        "host would let a Spot Demo signed request reach live Binance."
    )


def test_three_way_disjointness() -> None:
    """All 3 allowlists (PUBLIC, TESTNET, SPOT_DEMO) are pairwise disjoint."""
    public_testnet = PUBLIC_HOSTS & TESTNET_HOSTS
    public_demo = PUBLIC_HOSTS & SPOT_DEMO_HOSTS
    testnet_demo = TESTNET_HOSTS & SPOT_DEMO_HOSTS
    assert public_testnet == set(), f"PUBLIC ∩ TESTNET = {public_testnet}"
    assert public_demo == set(), f"PUBLIC ∩ SPOT_DEMO = {public_demo}"
    assert testnet_demo == set(), f"TESTNET ∩ SPOT_DEMO = {testnet_demo}"


@pytest.mark.parametrize("host", ["demo-api.binance.com"])
def test_spot_demo_hosts_accepted(host: str) -> None:
    """Allowed Spot Demo hosts pass the module-level check."""
    assert_spot_demo_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "testnet.binance.vision",  # Spot Testnet — different env
        "stream.testnet.binance.vision",  # Spot Testnet WS — different env
        "api.binance.com",  # live spot — catastrophic if accepted
        "fapi.binance.com",  # live futures — not in scope
        "demo-fapi.binance.com",  # Futures Demo — ROB-291 scope, not here
        "testnet.binancefuture.com",  # Futures testnet — not in scope
        "stream.binance.com",  # live public stream
        "api.binance.us",  # different exchange
        "evil.example.com",  # arbitrary
        "demo-api.binance.com.evil.example",  # subdomain spoof
    ],
)
def test_non_spot_demo_hosts_rejected(host: str) -> None:
    """Anything outside SPOT_DEMO_HOSTS raises BinanceLiveHostBlocked."""
    with pytest.raises(BinanceLiveHostBlocked):
        assert_spot_demo_host(host)

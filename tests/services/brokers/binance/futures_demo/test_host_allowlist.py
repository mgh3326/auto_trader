"""ROB-298 PR 2 — Futures Demo host allowlist + disjointness guard."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
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


def test_futures_demo_hosts_only_demo_fapi() -> None:
    assert FUTURES_DEMO_HOSTS == frozenset({"demo-fapi.binance.com"})


def test_disjoint_from_spot_demo() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(SPOT_DEMO_HOSTS)


def test_disjoint_from_public() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(PUBLIC_HOSTS)


def test_disjoint_from_deprecated_testnet() -> None:
    assert FUTURES_DEMO_HOSTS.isdisjoint(_DEPRECATED_TESTNET_HOSTS)
    assert FUTURES_DEMO_HOSTS.isdisjoint(_DEPRECATED_FUTURES_TESTNET_HOSTS)


def test_deprecated_futures_testnet_disjoint_from_spot_testnet() -> None:
    assert _DEPRECATED_FUTURES_TESTNET_HOSTS.isdisjoint(_DEPRECATED_TESTNET_HOSTS)


def test_assert_passes_for_demo_fapi() -> None:
    assert_futures_demo_host("demo-fapi.binance.com")  # no raise


@pytest.mark.parametrize(
    "host",
    [
        "fapi.binance.com",  # live futures
        "api.binance.com",  # live spot
        "demo-api.binance.com",  # spot demo
        "testnet.binance.vision",  # deprecated spot testnet
        "testnet.binancefuture.com",  # deprecated futures testnet
        "demo-fapi.binance.com.evil.example",  # spoofed subdomain
    ],
)
def test_assert_rejects_non_demo_fapi(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_futures_demo_host(host)

"""ROB-317 — read-only public futures stream allowlist.

fstream.binance.com is read-allowed (unsigned market data) but
signed-blocked (the futures-demo signed transport rejects it). The two
purposes never share a host with any signed mutation allowlist.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.host_allowlist import (
    FUTURES_DEMO_HOSTS,
    assert_futures_demo_host,
)
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
    PUBLIC_HOSTS,
    assert_public_futures_stream_host,
)
from app.services.brokers.binance.spot_demo.host_allowlist import SPOT_DEMO_HOSTS


def test_only_fstream() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS == frozenset({"fstream.binance.com"})


def test_disjoint_from_signed_mutation_allowlists() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(FUTURES_DEMO_HOSTS)
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(SPOT_DEMO_HOSTS)


def test_disjoint_from_public_spot_stream_allowlist() -> None:
    assert PUBLIC_FUTURES_STREAM_HOSTS.isdisjoint(PUBLIC_HOSTS)


def test_signed_futures_transport_still_rejects_fstream() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_futures_demo_host("fstream.binance.com")


def test_assert_accepts_fstream() -> None:
    assert_public_futures_stream_host("fstream.binance.com")  # no raise


@pytest.mark.parametrize(
    "host",
    [
        "fapi.binance.com",  # live signed futures
        "demo-fapi.binance.com",  # demo signed futures (mutation lane)
        "stream.binance.com",  # spot public stream
        "fstream.binance.com.evil.example",  # spoofed subdomain
    ],
)
def test_assert_rejects_non_fstream(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_public_futures_stream_host(host)

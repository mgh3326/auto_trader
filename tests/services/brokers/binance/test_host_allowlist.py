"""ROB-285 — Binance public host allowlist."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_HOSTS,
    assert_allowed_host,
)


@pytest.mark.parametrize(
    "host",
    [
        "api.binance.com",
        "data-api.binance.vision",
        "stream.binance.com",
        "data-stream.binance.vision",
    ],
)
def test_public_hosts_accepted(host: str) -> None:
    # Should not raise.
    assert_allowed_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "testnet.binance.vision",  # testnet — not in public adapter scope
        "fapi.binance.com",  # futures live — not in scope
        "api.binance.us",  # different exchange
        "evil.example.com",  # arbitrary
        "stream.binance.com.evil.example",  # subdomain spoof
    ],
)
def test_non_public_hosts_rejected(host: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_allowed_host(host)


def test_public_hosts_is_frozen() -> None:
    # Defense against accidental in-place mutation.
    assert isinstance(PUBLIC_HOSTS, frozenset)

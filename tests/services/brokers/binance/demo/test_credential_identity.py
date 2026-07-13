"""ROB-844 credential binding never persists or exposes raw credentials."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.demo.credential_identity import (
    demo_credential_fingerprint,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


def test_credential_fingerprint_is_deterministic_opaque_and_key_specific() -> None:
    raw_key = "RAW_DEMO_API_KEY_MUST_NEVER_PERSIST"
    fingerprint = demo_credential_fingerprint(raw_key)

    assert fingerprint == demo_credential_fingerprint(raw_key)
    assert fingerprint != demo_credential_fingerprint(raw_key + "-rotated")
    assert fingerprint.startswith("sha256:")
    assert raw_key not in fingerprint


@pytest.mark.asyncio
async def test_spot_and_futures_clients_expose_only_opaque_fingerprint() -> None:
    raw_key = "RAW_SHARED_DEMO_API_KEY_MUST_NEVER_PERSIST"
    raw_secret = "RAW_DEMO_SECRET_MUST_NEVER_PERSIST"
    spot = BinanceSpotDemoExecutionClient(api_key=raw_key, api_secret=raw_secret)
    futures = BinanceFuturesDemoExecutionClient(api_key=raw_key, api_secret=raw_secret)
    try:
        assert spot.credential_fingerprint == futures.credential_fingerprint
        evidence = {
            "credential_fingerprint": spot.credential_fingerprint,
        }
        rendered = repr(evidence)
        assert raw_key not in rendered
        assert raw_secret not in rendered
    finally:
        await spot.aclose()
        await futures.aclose()

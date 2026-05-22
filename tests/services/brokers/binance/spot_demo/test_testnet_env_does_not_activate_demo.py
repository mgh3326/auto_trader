"""ROB-298 — BINANCE_TESTNET_* must not activate Spot Demo trading."""
from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


def test_only_testnet_env_does_not_enable_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINANCE_SPOT_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_testnet_creds_do_not_substitute_for_demo_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        BinanceSpotDemoExecutionClient.from_env()

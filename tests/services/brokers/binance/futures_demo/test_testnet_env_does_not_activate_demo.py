"""ROB-298 PR 2 — BINANCE_TESTNET_* must not activate Futures Demo trading."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)


def test_only_testnet_env_does_not_enable_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BINANCE_TESTNET_* env vars alone do not activate Futures Demo."""
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        BinanceFuturesDemoExecutionClient.from_env()


def test_testnet_creds_do_not_substitute_for_futures_demo_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BINANCE_TESTNET_* creds cannot substitute for Futures Demo creds."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        BinanceFuturesDemoExecutionClient.from_env()

"""ROB-298 PR 2 — BINANCE_SPOT_DEMO_* must not activate Futures Demo trading."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)


def test_spot_demo_env_does_not_enable_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BINANCE_SPOT_DEMO_* env vars alone do not activate Futures Demo."""
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-demo-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-demo-secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        BinanceFuturesDemoExecutionClient.from_env()


def test_spot_demo_creds_do_not_substitute_for_futures_demo_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BINANCE_SPOT_DEMO_* creds cannot substitute for Futures Demo creds."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-demo-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-demo-secret")
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        BinanceFuturesDemoExecutionClient.from_env()

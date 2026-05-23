"""ROB-302 — Futures Demo from_env resolves via the shared canonical pair."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.futures_demo.preflight import (
    FuturesDemoPreflightClient,
)

_VARS = [
    "BINANCE_FUTURES_DEMO_API_KEY",
    "BINANCE_FUTURES_DEMO_API_SECRET",
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
]


@pytest.fixture(autouse=True)
def _enabled_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize(
    "factory",
    [FuturesDemoPreflightClient.from_env, BinanceFuturesDemoExecutionClient.from_env],
)
def test_canonical_demo_pair_constructs_client(monkeypatch, factory):
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    client = factory()
    assert client is not None


@pytest.mark.parametrize(
    "factory",
    [FuturesDemoPreflightClient.from_env, BinanceFuturesDemoExecutionClient.from_env],
)
def test_futures_specific_pair_still_constructs(monkeypatch, factory):
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "fut-key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "fut-secret")
    client = factory()
    assert client is not None


@pytest.mark.parametrize(
    "factory",
    [FuturesDemoPreflightClient.from_env, BinanceFuturesDemoExecutionClient.from_env],
)
def test_partial_override_fails_closed(monkeypatch, factory):
    """Half-set product override must fail closed (lane-specific error)."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "fut-key")
    # secret missing; canonical present must NOT backfill
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        factory()


@pytest.mark.parametrize(
    "factory",
    [FuturesDemoPreflightClient.from_env, BinanceFuturesDemoExecutionClient.from_env],
)
def test_canonical_does_not_bypass_enabled_gate(monkeypatch, factory):
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        factory()

"""ROB-302 — Spot Demo from_env resolves via the shared canonical pair.

Spot Demo is a deployed, working lane. These tests pin the additive contract:
behavior is byte-identical when BINANCE_SPOT_DEMO_* is set, and the canonical
BINANCE_DEMO_* pair is used only when the spot-specific pair is absent.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.preflight import SpotDemoPreflightClient

_VARS = [
    "BINANCE_SPOT_DEMO_API_KEY",
    "BINANCE_SPOT_DEMO_API_SECRET",
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
]


@pytest.fixture(autouse=True)
def _enabled_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize(
    "factory",
    [SpotDemoPreflightClient.from_env, BinanceSpotDemoExecutionClient.from_env],
)
def test_spot_specific_pair_still_constructs(monkeypatch, factory):
    """Regression: deployed contract unchanged when SPOT_* set."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "spot-secret")
    assert factory() is not None


@pytest.mark.parametrize(
    "factory",
    [SpotDemoPreflightClient.from_env, BinanceSpotDemoExecutionClient.from_env],
)
def test_canonical_demo_pair_constructs_client(monkeypatch, factory):
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    assert factory() is not None


@pytest.mark.parametrize(
    "factory",
    [SpotDemoPreflightClient.from_env, BinanceSpotDemoExecutionClient.from_env],
)
def test_partial_override_fails_closed(monkeypatch, factory):
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        factory()


@pytest.mark.parametrize(
    "factory",
    [SpotDemoPreflightClient.from_env, BinanceSpotDemoExecutionClient.from_env],
)
def test_canonical_does_not_bypass_enabled_gate(monkeypatch, factory):
    monkeypatch.delenv("BINANCE_SPOT_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "canon-key")
    monkeypatch.setenv("BINANCE_DEMO_API_SECRET", "canon-secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        factory()

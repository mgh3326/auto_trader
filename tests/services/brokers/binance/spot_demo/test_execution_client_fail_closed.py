"""ROB-298 — BinanceSpotDemoExecutionClient must fail-closed in all unsafe modes.

Mirrors the testnet fail-closed contract under a separate exception
hierarchy. The Spot Demo execution client must refuse construction when:

  * ``BINANCE_SPOT_DEMO_ENABLED`` is unset or non-truthy.
  * API key or API secret is missing.
  * ``BINANCE_SPOT_DEMO_BASE_URL`` resolves to a non-Spot-Demo host
    (cross-allowlist guard — live and testnet hosts are refused at
    construction time).
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoCrossAllowlistViolation,
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a fully scrubbed env to isolate fail-closed paths."""
    for var in (
        "BINANCE_SPOT_DEMO_ENABLED",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_BASE_URL",
        "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_disabled_when_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BINANCE_SPOT_DEMO_ENABLED=false`` → BinanceSpotDemoDisabled."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "false")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (env unset) → adapter init refuses."""
    monkeypatch.delenv("BINANCE_SPOT_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        BinanceSpotDemoExecutionClient.from_env()


def test_missing_api_key_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API key missing → BinanceSpotDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_KEY", raising=False)
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        BinanceSpotDemoExecutionClient.from_env()


def test_missing_api_secret_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API secret missing → BinanceSpotDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.delenv("BINANCE_SPOT_DEMO_API_SECRET", raising=False)
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        BinanceSpotDemoExecutionClient.from_env()


def test_base_url_must_be_demo_host_rejects_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BINANCE_SPOT_DEMO_BASE_URL=https://api.binance.com`` → refused.

    PUBLIC_HOSTS membership escalates to
    ``BinanceSpotDemoCrossAllowlistViolation``.
    """
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://api.binance.com")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        BinanceSpotDemoExecutionClient.from_env()


def test_base_url_must_be_demo_host_rejects_testnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BINANCE_SPOT_DEMO_BASE_URL=https://testnet.binance.vision`` → refused.

    TESTNET_HOSTS membership escalates to
    ``BinanceSpotDemoCrossAllowlistViolation``.
    """
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://testnet.binance.vision")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        BinanceSpotDemoExecutionClient.from_env()


def test_base_url_must_be_demo_host_rejects_arbitrary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-allowlisted host → BinanceLiveHostBlocked at construction."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "https://evil.example.com")
    with pytest.raises(BinanceLiveHostBlocked):
        BinanceSpotDemoExecutionClient.from_env()


def test_secret_is_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret must never appear in repr/str (parallel to testnet contract)."""
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_SPOT_DEMO_API_KEY", "PLACEHOLDER_SPOT_DEMO_KEY_DO_NOT_LOG"
    )
    monkeypatch.setenv(
        "BINANCE_SPOT_DEMO_API_SECRET", "PLACEHOLDER_SPOT_DEMO_SECRET_DO_NOT_LOG"
    )
    client = BinanceSpotDemoExecutionClient.from_env()
    rendered = repr(client)
    assert "PLACEHOLDER_SPOT_DEMO_SECRET_DO_NOT_LOG" not in rendered
    assert "PLACEHOLDER_SPOT_DEMO_KEY_DO_NOT_LOG" not in rendered

"""ROB-296 — Env/config parsing fail-closed behavior for the Spot Demo client.

Covers:
  * Default-disabled when ``BINANCE_SPOT_DEMO_ENABLED`` is unset/false.
  * Missing credentials → ``BinanceSpotDemoMissingCredentials``.
  * Default base URL when ``BINANCE_SPOT_DEMO_BASE_URL`` is unset.
  * ``BINANCE_TESTNET_*`` env vars MUST NOT activate the Spot Demo path.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
)
from app.services.brokers.binance.spot_demo.preflight import SpotDemoPreflightClient


def _clear_spot_demo_and_testnet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BINANCE_SPOT_DEMO_ENABLED",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_BASE_URL",
        "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT",
        "BINANCE_TESTNET_ENABLED",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_TESTNET_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_from_env_unset_raises_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env at all → fail-closed."""
    _clear_spot_demo_and_testnet_env(monkeypatch)
    with pytest.raises(BinanceSpotDemoDisabled):
        SpotDemoPreflightClient.from_env()


@pytest.mark.parametrize("falsy", ["", "false", "no", "0", "off", "FALSE"])
def test_from_env_falsy_enabled_raises_disabled(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Falsy values for the gate are treated as disabled."""
    _clear_spot_demo_and_testnet_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", falsy)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceSpotDemoDisabled):
        SpotDemoPreflightClient.from_env()


def test_from_env_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate on, key empty → MissingCredentials."""
    _clear_spot_demo_and_testnet_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        SpotDemoPreflightClient.from_env()


def test_from_env_missing_api_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate on, secret empty → MissingCredentials."""
    _clear_spot_demo_and_testnet_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "")
    with pytest.raises(BinanceSpotDemoMissingCredentials):
        SpotDemoPreflightClient.from_env()


def test_from_env_default_base_url_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``BINANCE_SPOT_DEMO_BASE_URL`` is unset, the default Spot Demo host is used."""
    _clear_spot_demo_and_testnet_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    client = SpotDemoPreflightClient.from_env()
    try:
        assert str(client._client.base_url) == "https://demo-api.binance.com"
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_testnet_env_does_not_activate_spot_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BINANCE_TESTNET_*`` vars MUST NOT activate the Spot Demo path.

    Hermes review §3 isolation: env namespaces are strictly separate.
    Even if every testnet var is set, ``SpotDemoPreflightClient.from_env``
    still raises ``BinanceSpotDemoDisabled`` because it reads
    ``BINANCE_SPOT_DEMO_ENABLED`` only.
    """
    _clear_spot_demo_and_testnet_env(monkeypatch)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    monkeypatch.setenv("BINANCE_TESTNET_BASE_URL", "https://testnet.binance.vision")
    with pytest.raises(BinanceSpotDemoDisabled):
        SpotDemoPreflightClient.from_env()

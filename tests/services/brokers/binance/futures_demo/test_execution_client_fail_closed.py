"""ROB-298 PR 2 — BinanceFuturesDemoExecutionClient must fail-closed in all unsafe modes.

Mirrors the Spot Demo fail-closed contract under the Futures Demo
exception hierarchy. The execution client must refuse construction when:

  * ``BINANCE_FUTURES_DEMO_ENABLED`` is unset or non-truthy.
  * API key or API secret is missing.
  * ``BINANCE_FUTURES_DEMO_BASE_URL`` resolves to a non-Futures-Demo host
    (cross-allowlist guard — live spot, live futures, Spot Demo, deprecated
    testnet hosts are all refused at construction time).
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a fully scrubbed env to isolate fail-closed paths."""
    for var in (
        "BINANCE_FUTURES_DEMO_ENABLED",
        "BINANCE_FUTURES_DEMO_API_KEY",
        "BINANCE_FUTURES_DEMO_API_SECRET",
        "BINANCE_FUTURES_DEMO_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_disabled_when_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BINANCE_FUTURES_DEMO_ENABLED=false`` → BinanceFuturesDemoDisabled."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "false")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        BinanceFuturesDemoExecutionClient.from_env()


def test_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (env unset) → adapter init refuses."""
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        BinanceFuturesDemoExecutionClient.from_env()


def test_missing_api_key_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API key missing → BinanceFuturesDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        BinanceFuturesDemoExecutionClient.from_env()


def test_missing_api_secret_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API secret missing → BinanceFuturesDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        BinanceFuturesDemoExecutionClient.from_env()


def test_base_url_rejects_live_futures(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BINANCE_FUTURES_DEMO_BASE_URL=https://fapi.binance.com`` → refused.

    Live USD-M Futures host membership escalates to
    ``BinanceFuturesDemoCrossAllowlistViolation`` (one-character typo away
    from the Futures Demo host).
    """
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://fapi.binance.com")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        BinanceFuturesDemoExecutionClient.from_env()


def test_base_url_rejects_spot_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BINANCE_FUTURES_DEMO_BASE_URL=https://demo-api.binance.com`` → refused.

    Spot Demo membership escalates to ``BinanceFuturesDemoCrossAllowlistViolation``
    because Spot Demo is a sibling demo lane — leakage MUST NOT happen.
    """
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-api.binance.com")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        BinanceFuturesDemoExecutionClient.from_env()


def test_base_url_rejects_arbitrary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-allowlisted host → BinanceLiveHostBlocked at construction."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://evil.example.com")
    with pytest.raises(BinanceLiveHostBlocked):
        BinanceFuturesDemoExecutionClient.from_env()


def test_secret_is_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret must never appear in repr/str (parallel to Spot Demo contract)."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_KEY", "PLACEHOLDER_FUTURES_DEMO_KEY_DO_NOT_LOG"
    )
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_SECRET", "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG"
    )
    client = BinanceFuturesDemoExecutionClient.from_env()
    rendered = repr(client)
    assert "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG" not in rendered
    assert "PLACEHOLDER_FUTURES_DEMO_KEY_DO_NOT_LOG" not in rendered

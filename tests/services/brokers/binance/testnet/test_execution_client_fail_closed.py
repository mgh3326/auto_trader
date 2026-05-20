"""ROB-286 — Adapter init fail-closed tests.

Matrix rows T5, T10, T11, T12, T16.
"""

from __future__ import annotations

import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.testnet.errors import (
    BinanceMissingCredentials,
    BinanceTestnetDisabled,
)
from app.services.brokers.binance.testnet.execution_client import (
    BinanceTestnetExecutionClient,
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    """Each test starts from a fully scrubbed env to isolate fail-closed paths."""
    for var in (
        "BINANCE_TESTNET_ENABLED",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_TESTNET_BASE_URL",
        "BINANCE_TESTNET_MAX_NOTIONAL_USDT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_disabled_by_default_raises_on_construct(monkeypatch) -> None:
    """T10 — Default (env unset) → adapter init refuses."""
    # Credentials set but enabled unset.
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with pytest.raises(BinanceTestnetDisabled):
        BinanceTestnetExecutionClient.from_env()


def test_missing_api_key_raises(monkeypatch) -> None:
    """T11 — API key missing → BinanceMissingCredentials."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    with pytest.raises(BinanceMissingCredentials):
        BinanceTestnetExecutionClient.from_env()


def test_missing_api_secret_raises(monkeypatch) -> None:
    """T12 — API secret missing → BinanceMissingCredentials."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    with pytest.raises(BinanceMissingCredentials):
        BinanceTestnetExecutionClient.from_env()


def test_env_base_url_pointing_to_live_raises(monkeypatch) -> None:
    """T5 — env BINANCE_TESTNET_BASE_URL=https://api.binance.com → init raises."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    monkeypatch.setenv("BINANCE_TESTNET_BASE_URL", "https://api.binance.com")
    with pytest.raises(BinanceLiveHostBlocked):
        BinanceTestnetExecutionClient.from_env()


def test_notional_override_without_reason_raises(monkeypatch) -> None:
    """T16 — Notional override requires an explicit reason argument."""
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "DUMMY_SECRET")
    client = BinanceTestnetExecutionClient.from_env()
    from decimal import Decimal

    with pytest.raises(ValueError):
        # Override above the default (10 USDT) without reason → ValueError.
        client._validate_notional(
            notional_usdt=Decimal("25"),
            override_reason=None,
        )
    # With a reason, the call is accepted.
    client._validate_notional(
        notional_usdt=Decimal("25"),
        override_reason="QA spike — coverage of order-book gap",
    )


def test_secret_is_not_in_repr(monkeypatch) -> None:
    """Hard reviewer focus area #8 — secret never appears in repr/str.

    Sentry/console operators must not be able to surface the secret
    through routine introspection.
    """
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "PLACEHOLDER_API_KEY_DO_NOT_LOG")
    monkeypatch.setenv(
        "BINANCE_TESTNET_API_SECRET", "SECRET_TESTNET_PLACEHOLDER_DO_NOT_LOG"
    )
    client = BinanceTestnetExecutionClient.from_env()
    rendered = repr(client)
    assert "SECRET_TESTNET_PLACEHOLDER_DO_NOT_LOG" not in rendered
    assert "PLACEHOLDER_API_KEY_DO_NOT_LOG" not in rendered


def test_secret_not_in_logs_on_init_failure(monkeypatch, caplog) -> None:
    """Hard reviewer focus area #8 — secret never appears in caplog.

    Force a fail-closed path and confirm the secret string is nowhere in
    the captured log output (covers both message and args).
    """
    import logging

    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "PLACEHOLDER_API_KEY_DO_NOT_LOG")
    # Provide a base_url that will fail at host check so init raises after
    # credentials are loaded — gives the codepath a chance to leak.
    monkeypatch.setenv(
        "BINANCE_TESTNET_API_SECRET", "SECRET_TESTNET_PLACEHOLDER_DO_NOT_LOG"
    )
    monkeypatch.setenv("BINANCE_TESTNET_BASE_URL", "https://api.binance.com")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BinanceLiveHostBlocked):
            BinanceTestnetExecutionClient.from_env()
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SECRET_TESTNET_PLACEHOLDER_DO_NOT_LOG" not in full_log, (
        f"Secret leaked into log output. Captured: {full_log!r}"
    )

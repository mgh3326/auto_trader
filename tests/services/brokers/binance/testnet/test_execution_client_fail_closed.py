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


def test_secret_not_in_logs_during_stop_placement_failure(monkeypatch, caplog) -> None:
    """TT14 — API secret never appears in logs during a stop-order placement
    failure.

    Reviewer focus #7 in the plan: on a 4xx broker reject, ensure the
    captured log message includes the broker error code but NOT the
    signed query string (which contains ``signature=``, an HMAC of the
    secret — even leaking the HMAC is a concern). We use a synthetic
    secret string that pytest can grep for in caplog records.
    """
    import logging
    import re
    from decimal import Decimal

    import httpx
    import pytest_asyncio  # noqa: F401 — confirms env wiring

    from app.services.brokers.binance.testnet.execution_client import (
        BinanceTestnetExecutionClient,
    )

    secret_str = "PLACEHOLDER_SECRET_DO_NOT_LOG_ROB289"
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "DUMMY_KEY")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", secret_str)
    client = BinanceTestnetExecutionClient.from_env()

    async def _run_and_capture():
        # Use httpx_mock-style patching directly via monkeypatch on the
        # client's transport. We force the underlying httpx POST to raise
        # a 4xx so the runner-side anomaly path is exercised.
        from unittest.mock import AsyncMock

        request = httpx.Request("POST", "https://testnet.binance.vision/api/v3/order")
        response = httpx.Response(
            400, json={"code": -2010, "msg": "rejected"}, request=request
        )

        async def _post(*args, **kwargs):
            response._request = request
            response.raise_for_status()
            return response  # unreachable

        client._client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "rejected",
                request=request,
                response=response,
            )
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(httpx.HTTPStatusError):
                await client.place_stop_limit_order(
                    symbol="BTCUSDT",
                    side="SELL",
                    quantity=Decimal("0.001"),
                    stop_price=Decimal("50500"),
                    limit_price=Decimal("50500"),
                    client_order_id="tp-leg-secret-test",
                    dry_run=False,
                    confirm=True,
                )

    import asyncio

    asyncio.run(_run_and_capture())
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert secret_str not in full_log, (
        f"Secret leaked into log output during stop placement failure: {full_log!r}"
    )
    # Also defend against a signature-like leak (HMAC hex value).
    # The HMAC of the secret would appear as ``signature=<64-hex>``; ensure
    # no such pattern appears in caplog.
    assert not re.search(r"signature=[0-9a-f]{64}", full_log), (
        f"HMAC signature leaked in logs: {full_log!r}"
    )


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

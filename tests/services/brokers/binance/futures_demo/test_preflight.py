"""ROB-298 PR 2 — Futures Demo preflight (signed GET /fapi/v1/account).

Mirrors the Spot Demo preflight contract under an independent fail-closed
exception hierarchy. The preflight is read-only: ONE signed HTTP GET
against ``demo-fapi.binance.com``, no DB writes, no order placement, no
scheduler activation.

Contract coverage:
  * ``from_env()`` refuses construction when:
      - ``BINANCE_FUTURES_DEMO_ENABLED`` is unset / false → ``BinanceFuturesDemoDisabled``
      - API key or secret is missing → ``BinanceFuturesDemoMissingCredentials``
      - ``BINANCE_FUTURES_DEMO_BASE_URL`` resolves to a non-Futures-Demo host
        (Spot Demo / Spot Testnet / Live spot / Live futures / arbitrary)
  * Signed GET ``/fapi/v1/account`` returns a redacted summary on 200.
  * Returned dataclass has expected source/venue/product = futures_demo/binance/usdm_futures.
  * Secret never appears in repr/caplog (regression sentinel).
  * ``aclose()`` shuts down the httpx client cleanly.
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoCrossAllowlistViolation,
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoMissingCredentials,
    BinanceFuturesDemoUnsupportedAuth,
)
from app.services.brokers.binance.futures_demo.preflight import (
    FuturesDemoPreflightClient,
    FuturesDemoPreflightResult,
)

_FUTURES_DEMO_BASE = "https://demo-fapi.binance.com"


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


# --------------------------------------------------------------------------- #
# from_env() fail-closed paths                                                #
# --------------------------------------------------------------------------- #


def test_disabled_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (env unset) → BinanceFuturesDemoDisabled."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        FuturesDemoPreflightClient.from_env()


def test_disabled_when_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BINANCE_FUTURES_DEMO_ENABLED=false`` → BinanceFuturesDemoDisabled."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "false")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    with pytest.raises(BinanceFuturesDemoDisabled):
        FuturesDemoPreflightClient.from_env()


def test_missing_api_key_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API key missing → BinanceFuturesDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_KEY", raising=False)
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        FuturesDemoPreflightClient.from_env()


def test_missing_api_secret_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env on but API secret missing → BinanceFuturesDemoMissingCredentials."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.delenv("BINANCE_FUTURES_DEMO_API_SECRET", raising=False)
    with pytest.raises(BinanceFuturesDemoMissingCredentials):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_live_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live spot host → BinanceFuturesDemoCrossAllowlistViolation."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://api.binance.com")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_live_futures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live USD-M Futures host (the dangerous one-char typo) → cross-allowlist."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://fapi.binance.com")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_spot_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spot Demo sibling host → BinanceFuturesDemoCrossAllowlistViolation."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-api.binance.com")
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_spot_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deprecated Spot Testnet host → BinanceFuturesDemoCrossAllowlistViolation."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_BASE_URL", "https://testnet.binance.vision"
    )
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_futures_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deprecated Futures Testnet host → BinanceFuturesDemoCrossAllowlistViolation."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_BASE_URL", "https://testnet.binancefuture.com"
    )
    with pytest.raises(BinanceFuturesDemoCrossAllowlistViolation):
        FuturesDemoPreflightClient.from_env()


def test_base_url_rejects_arbitrary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-allowlisted host → BinanceLiveHostBlocked at construction."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", "https://evil.example.com")
    with pytest.raises(BinanceLiveHostBlocked):
        FuturesDemoPreflightClient.from_env()


# --------------------------------------------------------------------------- #
# Signed GET /fapi/v1/account success path                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FuturesDemoPreflightClient:
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "DUMMY_FUTURES_DEMO_KEY")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "DUMMY_FUTURES_DEMO_SECRET")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)
    return FuturesDemoPreflightClient.from_env()


@pytest.mark.asyncio
async def test_preflight_account_success(
    client: FuturesDemoPreflightClient, httpx_mock
) -> None:
    """``preflight_account`` hits /fapi/v1/account and returns a redacted summary."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/account\?.*$"),
        status_code=200,
        json={
            "canTrade": True,
            "canDeposit": True,
            "canWithdraw": False,
            "accountType": "FUTURES",
            "totalWalletBalance": "1000.00000000",
            "assets": [
                {"asset": "USDT", "walletBalance": "500.00000000"},
                {"asset": "BNB", "walletBalance": "0.00000000"},
                {"asset": "BTC", "walletBalance": "0.10000000"},
            ],
            "positions": [
                {
                    "symbol": "XRPUSDT",
                    "positionAmt": "10.0",
                    "entryPrice": "0.5",
                    "unrealizedProfit": "0.1",
                },
                {
                    "symbol": "DOGEUSDT",
                    "positionAmt": "0",
                    "entryPrice": "0.0",
                    "unrealizedProfit": "0.0",
                },
            ],
        },
    )
    result = await client.preflight_account()
    assert isinstance(result, FuturesDemoPreflightResult)
    assert result.source == "futures_demo"
    assert result.venue == "binance"
    assert result.product == "usdm_futures"
    assert result.base_url == _FUTURES_DEMO_BASE
    assert result.account_can_trade is True
    assert result.account_can_deposit is True
    assert result.account_can_withdraw is False
    assert result.account_type == "FUTURES"
    # two assets nonzero (USDT, BTC); BNB is zero
    assert result.assets_nonzero_count == 2
    # one position with nonzero amount (XRPUSDT)
    assert result.positions_nonzero_count == 1

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    url_str = str(requests[0].url)
    assert "/fapi/v1/account" in url_str
    assert "signature=" in url_str
    assert "timestamp=" in url_str

    await client.aclose()


@pytest.mark.asyncio
async def test_preflight_to_evidence_dict_shape(
    client: FuturesDemoPreflightClient, httpx_mock
) -> None:
    """``to_evidence_dict`` is JSON-serializable and has the expected shape."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/account\?.*$"),
        status_code=200,
        json={
            "canTrade": True,
            "canDeposit": True,
            "canWithdraw": True,
            "accountType": "FUTURES",
            "assets": [],
            "positions": [],
        },
    )
    result = await client.preflight_account()
    evidence = result.to_evidence_dict()
    assert evidence["source"] == "futures_demo"
    assert evidence["venue"] == "binance"
    assert evidence["product"] == "usdm_futures"
    assert evidence["base_url"] == _FUTURES_DEMO_BASE
    assert "api_key_fingerprint" in evidence
    # Fingerprint must not echo the dummy key wholesale
    assert evidence["api_key_fingerprint"] != "DUMMY_FUTURES_DEMO_KEY"
    assert evidence["account"]["can_trade"] is True
    assert evidence["account"]["account_type"] == "FUTURES"
    assert evidence["account"]["assets_nonzero_count"] == 0
    assert evidence["account"]["positions_nonzero_count"] == 0
    await client.aclose()


# --------------------------------------------------------------------------- #
# Auth-rejection mapping                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", [-2014, -2008, -1022])
@pytest.mark.asyncio
async def test_preflight_account_unsupported_auth_codes(
    client: FuturesDemoPreflightClient, httpx_mock, code: int
) -> None:
    """Server-side HMAC rejection codes → BinanceFuturesDemoUnsupportedAuth."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/account\?.*$"),
        status_code=401,
        json={"code": code, "msg": "Signature for this request is not valid."},
    )
    with pytest.raises(BinanceFuturesDemoUnsupportedAuth):
        await client.preflight_account()
    await client.aclose()


@pytest.mark.asyncio
async def test_preflight_account_other_4xx_raises_http_status_error(
    client: FuturesDemoPreflightClient, httpx_mock
) -> None:
    """Non-auth 4xx surfaces as ``httpx.HTTPStatusError``."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/account\?.*$"),
        status_code=429,
        json={"code": -1003, "msg": "Too many requests."},
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.preflight_account()
    await client.aclose()


# --------------------------------------------------------------------------- #
# Secret hygiene                                                               #
# --------------------------------------------------------------------------- #


def test_secret_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret + full api_key must never appear in repr/str."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_KEY", "PLACEHOLDER_FUTURES_DEMO_KEY_DO_NOT_LOG"
    )
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_SECRET", "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG"
    )
    client = FuturesDemoPreflightClient.from_env()
    rendered = repr(client)
    assert "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG" not in rendered
    assert "PLACEHOLDER_FUTURES_DEMO_KEY_DO_NOT_LOG" not in rendered


@pytest.mark.asyncio
async def test_secret_not_in_caplog_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression sentinel: api_secret must not leak into log output during 4xx."""
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_KEY", "PLACEHOLDER_FUTURES_DEMO_KEY_DO_NOT_LOG"
    )
    monkeypatch.setenv(
        "BINANCE_FUTURES_DEMO_API_SECRET", "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG"
    )
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_BASE_URL", _FUTURES_DEMO_BASE)
    client = FuturesDemoPreflightClient.from_env()
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/account\?.*$"),
        status_code=401,
        json={"code": -1022, "msg": "Signature for this request is not valid."},
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BinanceFuturesDemoUnsupportedAuth) as excinfo:
        await client.preflight_account()
    assert "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG" not in caplog.text
    assert "PLACEHOLDER_FUTURES_DEMO_SECRET_DO_NOT_LOG" not in str(excinfo.value)
    await client.aclose()


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aclose_idempotent(
    client: FuturesDemoPreflightClient,
) -> None:
    """``aclose()`` must work even with no requests issued; second call is a no-op."""
    await client.aclose()
    # second call shouldn't raise
    await client.aclose()

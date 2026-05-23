"""ROB-296 — Spot Demo HMAC signing chokepoint tests.

Mirrors the testnet signing tests so any divergence (e.g., accidental
algorithm change) surfaces here too. The Spot Demo signer is
intentionally duplicated; this test pins its independent contract.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from app.services.brokers.binance.spot_demo.signing import (
    BINANCE_SPOT_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)


def test_sign_request_params_canonical_hmac_sha256() -> None:
    """Fixed inputs produce the canonical HMAC-SHA256 hex signature."""
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "0.001",
        "price": "50000.00",
        "timestamp": 1700000000000,
        "recvWindow": BINANCE_SPOT_DEMO_RECV_WINDOW_MS,
    }
    signed = _sign_request_params(params=params.copy(), api_secret=api_secret)
    assert "signature" in signed
    expected_payload = urlencode(params)
    expected_sig = hmac.new(
        api_secret.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signed["signature"] == expected_sig


def test_sign_request_params_attaches_timestamp_when_missing() -> None:
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {"symbol": "ETHUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.01"}
    signed = _sign_request_params(params=dict(params), api_secret=api_secret)
    assert "timestamp" in signed
    assert isinstance(signed["timestamp"], int)
    assert signed["timestamp"] > 10**12


def test_sign_request_params_does_not_mutate_caller_dict() -> None:
    original = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.001",
    }
    snapshot = dict(original)
    _sign_request_params(params=original, api_secret="TEST_SECRET")
    assert original == snapshot


def test_sign_request_params_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        _sign_request_params(params={"symbol": "BTCUSDT"}, api_secret="")


def test_recv_window_matches_binance_default() -> None:
    """Spot Demo recv_window matches the documented Binance default (5000ms).

    Previously this asserted equality with ``testnet.signing.BINANCE_RECV_WINDOW_MS``;
    the testnet adapter was removed in ROB-298, so we pin the value
    directly to the Binance-documented default.
    """
    assert BINANCE_SPOT_DEMO_RECV_WINDOW_MS == 5000

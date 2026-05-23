"""ROB-298 PR 2 — Futures Demo HMAC signing chokepoint tests.

Mirrors the Spot Demo signing tests (``tests/services/brokers/binance/
spot_demo/test_signing.py``) so any divergence between the two sibling
signers surfaces here too. The Futures Demo signer is intentionally
duplicated from Spot Demo (see ROB-296 Hermes review §1, Option A:
environment-specific fail-closed isolation outweighs deduplication).
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from app.services.brokers.binance.futures_demo.signing import (
    BINANCE_FUTURES_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)


def test_recv_window_matches_binance_default() -> None:
    """Futures Demo recv_window matches the documented Binance default (5000ms)."""
    assert BINANCE_FUTURES_DEMO_RECV_WINDOW_MS == 5000


def test_sign_request_params_canonical_hmac_sha256() -> None:
    """Fixed inputs produce the canonical HMAC-SHA256 hex signature."""
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {
        "symbol": "XRPUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "10",
        "price": "0.50",
        "timestamp": 1700000000000,
        "recvWindow": BINANCE_FUTURES_DEMO_RECV_WINDOW_MS,
    }
    signed = _sign_request_params(params=params.copy(), api_secret=api_secret)
    assert "signature" in signed
    # Signature is a 64-char hex string (sha256).
    assert len(signed["signature"]) == 64
    expected_payload = urlencode(params)
    expected_sig = hmac.new(
        api_secret.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signed["signature"] == expected_sig


def test_sign_request_params_attaches_timestamp_when_missing() -> None:
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {"symbol": "XRPUSDT", "side": "SELL", "type": "MARKET", "quantity": "10"}
    signed = _sign_request_params(params=dict(params), api_secret=api_secret)
    assert "timestamp" in signed
    assert isinstance(signed["timestamp"], int)
    assert signed["timestamp"] > 10**12


def test_sign_request_params_does_not_mutate_caller_dict() -> None:
    original = {
        "symbol": "XRPUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "10",
    }
    snapshot = dict(original)
    _sign_request_params(params=original, api_secret="TEST_SECRET")
    assert original == snapshot


def test_signature_is_deterministic_for_same_inputs() -> None:
    """Same params + same secret + same timestamp → same signature."""
    api_secret = "TEST_SECRET"
    params = {"symbol": "XRPUSDT", "timestamp": 1700000000000}
    s1 = _sign_request_params(params=dict(params), api_secret=api_secret)
    s2 = _sign_request_params(params=dict(params), api_secret=api_secret)
    assert s1["signature"] == s2["signature"]


def test_signature_differs_for_different_secrets() -> None:
    """Different secrets yield different signatures for identical params."""
    params = {"symbol": "XRPUSDT", "timestamp": 1700000000000}
    s1 = _sign_request_params(params=dict(params), api_secret="secret-A")
    s2 = _sign_request_params(params=dict(params), api_secret="secret-B")
    assert s1["signature"] != s2["signature"]


def test_sign_request_params_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        _sign_request_params(params={"symbol": "XRPUSDT"}, api_secret="")

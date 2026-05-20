"""ROB-286 — HMAC signing chokepoint tests.

Matrix row T17.

Lean adopted for open item #1: ``binance_common.utils.hmac_hashing`` is
the SDK-provided standalone HMAC-SHA256 signer; wrap it in
``_sign_request_params`` so all signing in the codebase goes through one
function. Verified callable here.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from app.services.brokers.binance.testnet.signing import (
    BINANCE_RECV_WINDOW_MS,
    _sign_request_params,
)


def test_sign_request_params_canonical() -> None:
    """T17 — Fixed inputs produce the canonical HMAC-SHA256 hex signature.

    The expected value is computed inline with the stdlib ``hmac`` module
    so the test pins the algorithm contract regardless of which signer
    implementation the wrapper uses today.
    """
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "0.001",
        "price": "50000.00",
        "timestamp": 1700000000000,
        "recvWindow": BINANCE_RECV_WINDOW_MS,
    }
    signed = _sign_request_params(params=params.copy(), api_secret=api_secret)
    # The signed dict must contain a 'signature' key whose value matches
    # HMAC-SHA256(secret, urlencode(params))
    assert "signature" in signed
    expected_payload = urlencode(params)
    expected_sig = hmac.new(
        api_secret.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signed["signature"] == expected_sig
    # Original params should be preserved (no key dropped, none added except signature).
    for k, v in params.items():
        assert signed[k] == v


def test_sign_request_params_attaches_timestamp_when_missing() -> None:
    """When caller omits ``timestamp``, the signer fills one in (ms).

    Binance signed endpoints all require ``timestamp``; the chokepoint is
    the right place to enforce it so call-sites can't forget.
    """
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    params = {
        "symbol": "ETHUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": "0.01",
    }
    signed = _sign_request_params(params=dict(params), api_secret=api_secret)
    assert "timestamp" in signed
    assert isinstance(signed["timestamp"], int)
    # Pinned millisecond resolution (Binance treats sub-second ts as 0).
    assert signed["timestamp"] > 10**12  # well after the year 2001 in ms


def test_sign_request_params_does_not_mutate_caller_dict() -> None:
    """The chokepoint returns a new dict; caller's params are untouched."""
    api_secret = "TEST_SECRET_DO_NOT_USE_IN_PROD"
    original = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.001",
    }
    snapshot = dict(original)
    _sign_request_params(params=original, api_secret=api_secret)
    assert original == snapshot, (
        "Signing chokepoint mutated caller's params dict — surprising and "
        "error-prone. It must return a new dict."
    )


def test_sign_request_params_rejects_empty_secret() -> None:
    """Defense in depth: empty/None secret raises."""
    import pytest

    with pytest.raises(ValueError):
        _sign_request_params(params={"symbol": "BTCUSDT"}, api_secret="")

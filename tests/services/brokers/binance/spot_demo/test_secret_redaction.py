"""ROB-296 — Secret redaction tests for the Spot Demo preflight client.

Guard against credentials leaking through ``repr``, logs, or evidence
JSON.
"""

from __future__ import annotations

import json

from app.services.brokers.binance.spot_demo.dry_run import plan_spot_demo_order
from app.services.brokers.binance.spot_demo.preflight import (
    SpotDemoPreflightClient,
    SpotDemoPreflightResult,
    _redact_api_key,
)

_API_KEY = "ROB296_TESTKEY_DEFINITELY_NOT_REAL_ABCDEF"
_API_SECRET = "ROB296_TESTSECRET_DEFINITELY_NOT_REAL_XYZ123"


def _close(client: SpotDemoPreflightClient) -> None:
    import asyncio

    asyncio.run(client.aclose())


def test_redact_api_key_short_returns_stars() -> None:
    assert _redact_api_key("ab") == "***"
    assert _redact_api_key("") == "***"


def test_redact_api_key_long_returns_fingerprint() -> None:
    """Long API keys are reduced to ``<first4>…<last2>`` fingerprint."""
    fp = _redact_api_key(_API_KEY)
    assert fp.startswith("ROB2")
    assert fp.endswith("AL"[-2:]) or fp.endswith("EF")
    # The full key never appears inside the fingerprint
    assert _API_KEY not in fp


def test_repr_does_not_contain_api_secret() -> None:
    client = SpotDemoPreflightClient(
        api_key=_API_KEY,
        api_secret=_API_SECRET,
        base_url="https://demo-api.binance.com",
    )
    try:
        rep = repr(client)
        assert _API_SECRET not in rep, (
            f"repr() leaked api_secret: {rep!r}. Secret-redaction failure."
        )
    finally:
        _close(client)


def test_repr_does_not_contain_raw_api_key() -> None:
    """The full api_key string must NOT appear in repr — fingerprint only."""
    client = SpotDemoPreflightClient(
        api_key=_API_KEY,
        api_secret=_API_SECRET,
        base_url="https://demo-api.binance.com",
    )
    try:
        rep = repr(client)
        assert _API_KEY not in rep, (
            f"repr() leaked raw api_key: {rep!r}. Use fingerprint instead."
        )
    finally:
        _close(client)


def test_evidence_dict_redacts_secret_and_full_key() -> None:
    """Preflight evidence dict has no full secret and no full key."""
    result = SpotDemoPreflightResult(
        source="spot_demo",
        venue="binance",
        product="spot",
        base_url="https://demo-api.binance.com",
        api_key_fingerprint=_redact_api_key(_API_KEY),
        account_can_trade=True,
        account_can_deposit=False,
        account_can_withdraw=False,
        account_type="SPOT",
        balances_nonzero_count=2,
    )
    payload = json.dumps(result.to_evidence_dict())
    assert _API_SECRET not in payload, "evidence JSON leaked api_secret"
    assert _API_KEY not in payload, "evidence JSON leaked raw api_key"


def test_plan_evidence_dict_has_no_secret_or_key_fields() -> None:
    """Planned-order evidence does not carry credential material at all."""
    from decimal import Decimal

    plan = plan_spot_demo_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        notional_cap_usdt=Decimal("10"),
    )
    payload = plan.to_evidence_dict()
    assert "api_key" not in payload
    assert "api_secret" not in payload
    assert "X-MBX-APIKEY" not in json.dumps(payload)


def test_source_venue_labels_on_evidence() -> None:
    """Both evidence types carry explicit source/venue labels."""
    from decimal import Decimal

    plan = plan_spot_demo_order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.001"),
        price=None,
        notional_cap_usdt=Decimal("10"),
    )
    plan_dict = plan.to_evidence_dict()
    assert plan_dict["source"] == "spot_demo"
    assert plan_dict["venue"] == "binance"
    assert plan_dict["product"] == "spot"

    result = SpotDemoPreflightResult(
        source="spot_demo",
        venue="binance",
        product="spot",
        base_url="https://demo-api.binance.com",
        api_key_fingerprint="abc…ef",
        account_can_trade=True,
        account_can_deposit=True,
        account_can_withdraw=True,
        account_type="SPOT",
        balances_nonzero_count=0,
    )
    result_dict = result.to_evidence_dict()
    assert result_dict["source"] == "spot_demo"
    assert result_dict["venue"] == "binance"
    assert result_dict["product"] == "spot"

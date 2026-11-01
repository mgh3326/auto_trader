"""The forwarder calls the existing KIS mock functions with their real signatures."""

from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest

from app.mcp_server.tooling import order_execution, orders_modify_cancel
from app.mcp_server.tooling.orders_modify_cancel import (
    _validate_cancel_inputs,
    _validate_modify_inputs,
)
from app.schemas.execution_intent import AmendChild, SubmitChild
from app.services.execution_intent_gateway.kis_mock_forwarder import KisMockForwarder
from tests.services.execution_intent_gateway.support import intent_snapshot


def _fake(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    real = getattr(module, name)
    captured: dict[str, Any] = {}

    async def fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
        inspect.signature(real).bind(*args, **kwargs)
        captured.update(kwargs)
        return result

    monkeypatch.setattr(module, name, fake)
    return captured


def _submit() -> SubmitChild:
    return SubmitChild(
        action="submit",
        intent_id=uuid4(),
        idempotency_key="child-0001",
        quantity=3,
        limit_price_krw=70_100,
    )


def _amend() -> AmendChild:
    return AmendChild(
        action="amend",
        intent_id=uuid4(),
        idempotency_key="child-0002",
        target_idempotency_key="child-0001",
        quantity=2,
        limit_price_krw=70_200,
    )


@pytest.mark.asyncio
async def test_submit_pins_mock_and_carries_the_intent_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _fake(
        monkeypatch,
        order_execution,
        "_place_order_impl",
        {"success": True, "order_no": "0000123"},
    )
    forwarded = await KisMockForwarder().submit(
        intent_snapshot(strategy="s3-test"), _submit()
    )
    assert captured["is_mock"] is True
    assert captured["dry_run"] is False
    assert captured["order_type"] == "limit"
    assert captured["strategy"] == "s3-test"
    assert captured["symbol"] == "005930"
    assert captured["market"] == "kr"
    assert (captured["quantity"], captured["price"]) == (3.0, 70_100.0)
    assert captured["correlation_id"] == "intent-gateway:child-0001"
    assert (forwarded.accepted, forwarded.broker_order_id) == (True, "0000123")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "reason": "broker_rejected"},
        {"success": False, "error_code": "attribution_required"},
        {"success": False, "retry_allowed": True},
        {"success": "true", "order_no": "1"},
        {},
    ],
)
async def test_submit_failure_shapes_are_not_accepted(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]
) -> None:
    _fake(monkeypatch, order_execution, "_place_order_impl", result)
    forwarded = await KisMockForwarder().submit(intent_snapshot(), _submit())
    assert forwarded.accepted is False


@pytest.mark.asyncio
async def test_amend_pins_mock_and_returns_the_replacement_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _fake(
        monkeypatch,
        orders_modify_cancel,
        "modify_order_impl",
        {"success": True, "status": "modified", "new_order_id": "0000456"},
    )
    forwarded = await KisMockForwarder().amend(
        intent_snapshot(), _amend(), broker_order_id="0000123"
    )
    assert captured["is_mock"] is True
    assert captured["dry_run"] is False
    assert captured["order_id"] == "0000123"
    assert (captured["new_price"], captured["new_quantity"]) == (70_200.0, 2.0)
    assert (forwarded.accepted, forwarded.broker_order_id) == (True, "0000456")


@pytest.mark.asyncio
async def test_cancel_pins_mock_and_uses_the_journaled_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _fake(
        monkeypatch, orders_modify_cancel, "cancel_order_impl", {"success": True}
    )
    forwarded = await KisMockForwarder().cancel(
        intent_snapshot(), broker_order_id="0000123"
    )
    assert captured["is_mock"] is True
    assert captured["order_id"] == "0000123"
    assert (captured["symbol"], captured["market"]) == ("005930", "kr")
    assert (forwarded.accepted, forwarded.broker_order_id) == (True, "0000123")


@pytest.mark.asyncio
async def test_cancel_refused_by_the_followup_capability_guard_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(
        monkeypatch,
        orders_modify_cancel,
        "cancel_order_impl",
        {"success": False, "broker_cancel_confirmed": False, "reason_code": "x"},
    )
    forwarded = await KisMockForwarder().cancel(
        intent_snapshot(), broker_order_id="0000123"
    )
    assert forwarded.accepted is False


def test_market_argument_resolves_to_equity_kr_in_the_real_validators() -> None:
    assert _validate_cancel_inputs("0000123", "005930", "kr")[2] == "equity_kr"
    assert _validate_modify_inputs("0000123", "005930", "kr", 70_000.0, 1.0)[2] == (
        "equity_kr"
    )

"""Acceptance 1: missing or oversized fields are refused."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.execution_intent import (
    MAX_INTENT_NOTIONAL_KRW,
    AmendChild,
    CancelChild,
    ExecutionIntentV1,
    SubmitChild,
    contract_digest,
    parse_child_request,
)
from app.services.kis_mock_runner.envelope import LOCKED_ENVELOPE

REQUIRED_INTENT_FIELDS = (
    "schema_version",
    "intent_id",
    "strategy",
    "account",
    "venue",
    "symbol",
    "side",
    "max_quantity",
    "max_notional_krw",
    "limit_price_krw",
    "max_slippage_bps",
    "expires_at",
    "inventory",
    "policy_version",
    "authorization_ref",
)


def intent_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "intent_id": str(uuid4()),
        "strategy": "s3-test",
        "account": "kis_mock",
        "venue": "krx",
        "symbol": "005930",
        "side": "buy",
        "max_quantity": 10,
        "max_notional_krw": 1_000_000,
        "limit_price_krw": 70_000,
        "max_slippage_bps": 50,
        "expires_at": "2026-11-02T06:30:00+00:00",
        "inventory": {
            "position_at_authorization": 0,
            "min_position_after": 0,
            "max_position_after": 10,
        },
        "policy_version": "policy-1",
        "model_version": "tft-3",
        "authorization_ref": "hk:2528",
        **overrides,
    }


def test_valid_intent_parses_and_is_immutable() -> None:
    intent = ExecutionIntentV1.model_validate(intent_payload())
    assert intent.max_quantity == 10
    with pytest.raises(ValidationError):
        intent.max_quantity = 11  # type: ignore[misc]


@pytest.mark.parametrize("field", REQUIRED_INTENT_FIELDS)
def test_intent_missing_field_is_refused(field: str) -> None:
    payload = intent_payload()
    del payload[field]
    with pytest.raises(ValidationError) as excinfo:
        ExecutionIntentV1.model_validate(payload)
    assert field in {error["loc"][0] for error in excinfo.value.errors()}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_quantity", 1_000_001),
        ("max_quantity", 0),
        ("max_quantity", 1.5),
        ("max_quantity", "10"),
        ("max_quantity", True),
        ("max_notional_krw", MAX_INTENT_NOTIONAL_KRW + 1),
        ("limit_price_krw", 10_000_001),
        ("limit_price_krw", 70_000.5),
        ("max_slippage_bps", 501),
        ("max_slippage_bps", -1),
        ("strategy", "s" * 129),
        ("policy_version", "p" * 129),
        ("model_version", "m" * 129),
        ("authorization_ref", "a" * 129),
        ("authorization_ref", ""),
        ("symbol", "0059300"),
        ("symbol", "BRK.B"),
    ],
)
def test_intent_oversized_or_malformed_field_is_refused(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(**{field: value}))


@pytest.mark.parametrize(
    "account", ["kis_live", "toss_live", "upbit", "alpaca_paper", "KIS_MOCK", ""]
)
def test_intent_refuses_any_account_outside_the_mock_list(account: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(account=account))


@pytest.mark.parametrize("strategy", ["unknown", "   ", "tbd", "test", "N/A"])
def test_intent_refuses_a_placeholder_strategy(strategy: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(strategy=strategy))


def test_intent_refuses_a_strategy_that_grows_past_the_limit_when_normalized() -> None:
    raw = "\ufdfa" * 128
    assert len(raw) == 128
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(strategy=raw))


def test_intent_refuses_naive_expiry_unknown_field_and_other_version() -> None:
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(
            intent_payload(expires_at="2026-11-02T06:30:00")
        )
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(extra_cap=1))
    with pytest.raises(ValidationError):
        ExecutionIntentV1.model_validate(intent_payload(schema_version=2))


def test_inventory_range_must_be_ordered_and_cannot_go_short() -> None:
    inverted = {
        "position_at_authorization": 0,
        "min_position_after": 5,
        "max_position_after": 1,
    }
    short = {
        "position_at_authorization": 0,
        "min_position_after": -1,
        "max_position_after": 1,
    }
    for inventory in (inverted, short):
        with pytest.raises(ValidationError):
            ExecutionIntentV1.model_validate(intent_payload(inventory=inventory))


def test_intent_notional_ceiling_equals_the_locked_single_order_cap() -> None:
    assert MAX_INTENT_NOTIONAL_KRW == LOCKED_ENVELOPE.max_single_notional_krw


def test_child_requests_parse_by_action() -> None:
    intent_id = str(uuid4())
    base = {"intent_id": intent_id, "idempotency_key": "child-0001"}
    order = {"quantity": 1, "limit_price_krw": 70_000}
    target = {"target_idempotency_key": "child-0000"}
    assert isinstance(
        parse_child_request({"action": "submit", **base, **order}), SubmitChild
    )
    assert isinstance(
        parse_child_request({"action": "amend", **base, **order, **target}), AmendChild
    )
    assert isinstance(
        parse_child_request({"action": "cancel", **base, **target}), CancelChild
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"idempotency_key": "short"},
        {"idempotency_key": "k" * 129},
        {"idempotency_key": "has space in it"},
        {"quantity": 1_000_001},
        {"quantity": 0},
        {"limit_price_krw": 10_000_001},
        {"action": "replace"},
        {"broker_order_id": "1234"},
    ],
)
def test_child_request_oversized_or_unknown_field_is_refused(
    changes: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "action": "submit",
        "intent_id": str(uuid4()),
        "idempotency_key": "child-0001",
        "quantity": 1,
        "limit_price_krw": 70_000,
        **changes,
    }
    with pytest.raises(ValidationError):
        parse_child_request(payload)


@pytest.mark.parametrize("missing", ["intent_id", "idempotency_key", "quantity"])
def test_child_request_missing_field_is_refused(missing: str) -> None:
    payload: dict[str, object] = {
        "action": "submit",
        "intent_id": str(uuid4()),
        "idempotency_key": "child-0001",
        "quantity": 1,
        "limit_price_krw": 70_000,
    }
    del payload[missing]
    with pytest.raises(ValidationError):
        parse_child_request(payload)


def test_amend_requires_both_new_values_and_cancel_takes_no_order_values() -> None:
    intent_id = str(uuid4())
    with pytest.raises(ValidationError):
        parse_child_request(
            {
                "action": "amend",
                "intent_id": intent_id,
                "idempotency_key": "child-0001",
                "target_idempotency_key": "child-0000",
                "limit_price_krw": 70_000,
            }
        )
    with pytest.raises(ValidationError):
        parse_child_request(
            {
                "action": "cancel",
                "intent_id": intent_id,
                "idempotency_key": "child-0001",
                "target_idempotency_key": "child-0000",
                "quantity": 1,
            }
        )


def test_contract_digest_changes_with_any_value() -> None:
    intent_id = uuid4()
    base = {
        "action": "submit",
        "intent_id": intent_id,
        "idempotency_key": "child-0001",
        "quantity": 1,
        "limit_price_krw": 70_000,
    }
    first = SubmitChild.model_validate(base)
    assert contract_digest(first) == contract_digest(SubmitChild.model_validate(base))
    assert contract_digest(first) != contract_digest(
        SubmitChild.model_validate({**base, "quantity": 2})
    )

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
    PaperReasonCode,
    PaperRiskSnapshot,
    VerifiedExperimentProvenance,
    VerifiedPaperOrderIntent,
    derive_paper_idempotency_key,
)


def _request_data() -> dict[str, object]:
    return {
        "intent_id": "intent-001",
        "experiment_id": "experiment-001",
        "run_id": "run-001",
        "cohort_id": "cohort-001",
        "strategy_version_id": "strategy-v1",
        "strategy_hash": "sha256:strategy",
        "config_hash": "sha256:config",
        "policy_hash": "sha256:policy",
        "venue": Broker.BINANCE,
        "account_mode": "demo",
        "product": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "order_type": "market",
        "time_in_force": None,
        "qty": None,
        "notional": Decimal("10"),
        "price": None,
        "market_snapshot_id": "snapshot-001",
        "market_snapshot_hash": "sha256:snapshot",
        "market_snapshot_as_of": datetime(2026, 7, 13, 1, 2, tzinfo=UTC),
        "market_snapshot_source": "binance_public_spot",
        "source_buy_reference": None,
    }


def _provenance() -> VerifiedExperimentProvenance:
    return VerifiedExperimentProvenance(
        **_request_data(),
        decision_id="decision-001",
        reference_price=Decimal("60000"),
        source_buy_client_order_id=None,
    )


@pytest.mark.parametrize(
    "field",
    [
        "intent_id",
        "experiment_id",
        "run_id",
        "cohort_id",
        "strategy_version_id",
        "strategy_hash",
        "config_hash",
        "policy_hash",
        "account_mode",
        "product",
        "symbol",
        "market_snapshot_id",
        "market_snapshot_hash",
        "market_snapshot_source",
    ],
)
def test_request_rejects_missing_or_blank_identity(field: str) -> None:
    data = _request_data()
    data[field] = " "

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_request_rejects_invalid_notional(value: Decimal) -> None:
    data = _request_data()
    data["notional"] = value

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)


def test_request_requires_exactly_one_sizing_mode() -> None:
    missing = _request_data()
    missing["notional"] = None
    both = _request_data()
    both["qty"] = Decimal("1")

    with pytest.raises(ValidationError):
        PaperOrderRequest(**missing)
    with pytest.raises(ValidationError):
        PaperOrderRequest(**both)


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("Infinity")])
def test_request_rejects_invalid_price(value: Decimal) -> None:
    data = _request_data()
    data["price"] = value

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)


def test_sell_requires_opaque_source_buy_reference() -> None:
    data = _request_data()
    data.update(
        venue=Broker.ALPACA,
        account_mode="paper",
        product="crypto",
        symbol="BTC/USD",
        side="sell",
        order_type="limit",
        time_in_force="gtc",
        qty=Decimal("0.001"),
        notional=None,
        price=Decimal("60000"),
    )

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)


def test_snapshot_timestamp_must_be_timezone_aware() -> None:
    data = _request_data()
    data["market_snapshot_as_of"] = datetime(2026, 7, 13, 1, 2)

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)


def test_caller_contract_forbids_origin_idempotency_and_unknown_fields() -> None:
    data = _request_data()
    data["origin"] = "manual"

    with pytest.raises(ValidationError):
        PaperOrderRequest(**data)

    assert "origin" not in PaperOrderRequest.model_fields
    assert "idempotency_key" not in PaperOrderRequest.model_fields


def test_verified_evidence_and_intent_are_frozen() -> None:
    provenance = _provenance()
    request = PaperOrderRequest(**_request_data())
    intent = VerifiedPaperOrderIntent(
        **request.model_dump(),
        decision_id=provenance.decision_id,
        reference_price=provenance.reference_price,
        source_buy_client_order_id=provenance.source_buy_client_order_id,
        origin="experiment",
        idempotency_key=derive_paper_idempotency_key(provenance),
    )

    with pytest.raises(ValidationError):
        provenance.decision_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        intent.origin = "manual"  # type: ignore[misc]


def test_idempotency_key_is_deterministic_bounded_and_identity_sensitive() -> None:
    first = _provenance()
    same = _provenance()
    changed = first.model_copy(update={"decision_id": "decision-002"})

    first_key = derive_paper_idempotency_key(first)

    assert first_key == derive_paper_idempotency_key(same)
    assert first_key != derive_paper_idempotency_key(changed)
    assert first_key.startswith("rob845-")
    assert len(first_key) <= 36


def test_operation_result_has_stable_unsupported_reason_and_is_frozen() -> None:
    result = PaperOperationResult.blocked(
        operation=PaperOperation.CANCEL,
        venue=Broker.BINANCE,
        reason_code=PaperReasonCode.UNSUPPORTED_CAPABILITY,
    )

    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code is PaperReasonCode.UNSUPPORTED_CAPABILITY
    assert result.model_dump(mode="json")["reason_code"] == "unsupported_capability"
    with pytest.raises(ValidationError):
        result.replayed = True  # type: ignore[misc]


def test_risk_snapshot_preserves_unknown_exposure_without_fabricating_zero() -> None:
    snapshot = PaperRiskSnapshot(
        open_exposure=None,
        reserved_notional=None,
        daily_realized_loss=Decimal("0"),
        quote_price=Decimal("60000"),
        spread_bps=Decimal("1.5"),
        data_age_seconds=Decimal("0.25"),
        quote_source="binance_public_spot",
        quote_as_of=datetime(2026, 7, 13, 1, 2, tzinfo=UTC),
        policy_version="policy-v1",
        policy_hash="sha256:policy",
    )

    assert snapshot.open_exposure is None
    assert snapshot.reserved_notional is None
    with pytest.raises(ValidationError):
        snapshot.quote_price = Decimal("1")  # type: ignore[misc]

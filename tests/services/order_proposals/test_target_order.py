from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.core.timezone import KST
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.target_order import TargetOrderSnapshot


def _broker_row(**overrides):
    return {
        "order_id": "manual-upbit-1",
        "symbol": "KRW-AVAX",
        "side": "SELL",
        "status": "partial",
        "remaining_qty": 3.500000,
        "ordered_price": 42000.0,
        "order_type": "LIMIT",
        **overrides,
    }


@pytest.mark.unit
def test_snapshot_normalizes_manual_broker_order():
    snapshot = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )

    assert snapshot.broker_order_id == "manual-upbit-1"
    assert snapshot.side == "sell"
    assert snapshot.order_type == "limit"
    assert snapshot.status == "open"
    assert snapshot.remaining_quantity == "3.5"
    assert snapshot.limit_price == "42000"
    assert snapshot.observed_at == "2026-07-11T08:23:00+00:00"


@pytest.mark.unit
def test_snapshot_defaults_missing_remaining_quantity_to_zero_for_closed_order():
    snapshot = TargetOrderSnapshot.from_broker_order(
        _broker_row(status="cancelled", remaining_qty=None),
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )

    assert snapshot.status == "cancelled"
    assert snapshot.remaining_quantity == "0"


@pytest.mark.unit
def test_snapshot_normalizes_observation_time_to_utc():
    snapshot = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 17, 23, tzinfo=KST),
    )

    assert snapshot.observed_at == "2026-07-11T08:23:00+00:00"


@pytest.mark.unit
def test_snapshot_payload_round_trip_reconstructs_canonical_object():
    snapshot = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )

    assert TargetOrderSnapshot.from_payload(snapshot.to_payload()) == snapshot


@pytest.mark.unit
def test_snapshot_is_frozen():
    snapshot = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.status = "cancelled"


@pytest.mark.unit
def test_matches_approved_ignores_observation_time_but_detects_remaining_drift():
    approved = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
    )
    same_order_later = TargetOrderSnapshot.from_broker_order(
        _broker_row(),
        observed_at=datetime(2026, 7, 11, 8, 24, tzinfo=UTC),
    )
    partially_filled = TargetOrderSnapshot.from_broker_order(
        _broker_row(remaining_qty=3),
        observed_at=datetime(2026, 7, 11, 8, 24, tzinfo=UTC),
    )

    assert approved.matches_approved(same_order_later) is True
    assert approved.matches_approved(partially_filled) is False


@pytest.mark.unit
@pytest.mark.parametrize("field", ["order_id", "symbol", "side", "order_type"])
def test_snapshot_rejects_missing_required_broker_fields(field):
    with pytest.raises(OrderProposalError, match=field):
        TargetOrderSnapshot.from_broker_order(
            _broker_row(**{field: "  "}),
            observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [("side", "hold"), ("order_type", "stop_limit")],
)
def test_snapshot_rejects_unsupported_side_or_order_type(field, value):
    with pytest.raises(OrderProposalError):
        TargetOrderSnapshot.from_broker_order(
            _broker_row(**{field: value}),
            observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
        )


@pytest.mark.unit
def test_snapshot_rejects_naive_observation_time():
    with pytest.raises(OrderProposalError, match="timezone-aware"):
        TargetOrderSnapshot.from_broker_order(
            _broker_row(),
            observed_at=datetime(2026, 7, 11, 8, 23),
        )


@pytest.mark.unit
def test_snapshot_rejects_open_order_without_positive_remaining_quantity():
    with pytest.raises(OrderProposalError, match="remaining quantity"):
        TargetOrderSnapshot.from_broker_order(
            _broker_row(status="open", remaining_qty=0),
            observed_at=datetime(2026, 7, 11, 8, 23, tzinfo=UTC),
        )

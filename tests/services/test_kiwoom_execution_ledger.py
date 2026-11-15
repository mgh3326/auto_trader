"""hk 4734 (3797 T-P4) — Kiwoom mock fill → execution_ledger row builder.

Pure unit tests: no database, no broker. They pin the row shape of design §2.3
(I9 fixed broker/account_mode, IV exact Decimals) and the delta idempotency the
kernel relies on (the same delta yields the same ``fill_seq``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.kiwoom_execution_ledger import (
    KIWOOM_ACCOUNT_MODE,
    KIWOOM_BROKER,
    KIWOOM_VENUE,
    build_kiwoom_execution_ledger_upsert,
)

pytestmark = [pytest.mark.unit]

OBSERVED_AT = datetime(2026, 11, 16, 3, tzinfo=UTC)


def _build(**overrides) -> ExecutionLedgerUpsert:
    kwargs = {
        "ledger_id": 7,
        "broker_order_id": "000123",
        "symbol": "005930",
        "side": "buy",
        "previous_filled_qty": Decimal("0"),
        "filled_quantity": 10,
        "average_price": 70000,
        "observed_at": OBSERVED_AT,
        "correlation_id": "kiwoom-mock:abc",
    }
    kwargs.update(overrides)
    return build_kiwoom_execution_ledger_upsert(**kwargs)


def test_row_shape_is_fixed_by_design() -> None:
    fill = _build()

    # I9 — the broker/account mode are fixed, not caller-controlled.
    assert fill.broker == KIWOOM_BROKER == "kiwoom"
    assert fill.account_mode == KIWOOM_ACCOUNT_MODE == "mock"
    assert fill.venue == KIWOOM_VENUE == "krx"
    assert fill.instrument_type == "equity_kr"
    assert fill.currency == "KRW"
    assert fill.source == "reconciler"
    # The mock TRs report neither a fee nor a fill time: say so, never zero.
    assert fill.fee_amount is None
    assert fill.tax_amount is None
    assert fill.fee_basis == "not_provided"
    assert fill.filled_at_basis == "observed_at"
    assert fill.filled_at == OBSERVED_AT
    assert fill.correlation_id == "kiwoom-mock:abc"


def test_quantity_is_the_delta_and_prices_are_exact_decimals() -> None:
    fill = _build(
        previous_filled_qty=Decimal("4"),
        previous_average_price=Decimal("70000"),
        filled_quantity=10,
    )

    assert fill.filled_qty == Decimal("6")
    assert fill.filled_price == Decimal("70000")
    assert fill.filled_notional == Decimal("420000")
    # Exact decimal arithmetic: no float ever touched the values.
    assert isinstance(fill.filled_qty, Decimal)
    assert isinstance(fill.filled_price, Decimal)


def test_incremental_price_comes_from_the_cumulative_notional_difference() -> None:
    # 4 @ 70,000 booked first; the broker now reports cumulative 10 @ 76,000.
    # The second row must be the real 6 @ 80,000, not 6 @ the 76,000 VWAP.
    first = _build(previous_filled_qty=Decimal("0"), filled_quantity=4)
    second = _build(
        previous_filled_qty=Decimal("4"),
        previous_average_price=Decimal("70000"),
        filled_quantity=10,
        average_price=76000,
    )

    assert second.filled_qty == Decimal("6")
    assert second.filled_price == Decimal("80000")
    assert second.filled_notional == Decimal("480000")
    # Booked notionals sum to the broker's cumulative notional (10 * 76,000).
    assert first.filled_notional + second.filled_notional == Decimal(10) * Decimal(
        "76000"
    )


def test_same_delta_is_one_identity_and_a_new_delta_is_another() -> None:
    first = _build()
    replay = _build()
    second = _build(
        previous_filled_qty=Decimal("10"),
        previous_average_price=Decimal("70000"),
        filled_quantity=12,
        average_price=72000,
    )

    assert first.fill_seq == replay.fill_seq
    assert first.fill_seq != second.fill_seq


def test_fill_seq_is_stable_across_calls() -> None:
    # A fixed seed rather than an object identity: the value survives restarts,
    # so a re-run of the same broker evidence cannot book a second row.
    assert _build().fill_seq == _build().fill_seq


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"filled_quantity": 0}, ValueError),
        ({"average_price": 0}, ValueError),
        ({"average_price": -1}, ValueError),
        ({"previous_filled_qty": Decimal("10"), "filled_quantity": 10}, ValueError),
        ({"previous_filled_qty": Decimal("10"), "filled_quantity": 9}, ValueError),
        ({"broker_order_id": "  "}, ValueError),
        ({"filled_quantity": True}, TypeError),
        ({"filled_quantity": "10"}, TypeError),
        ({"average_price": 70000.0}, TypeError),
        # An earlier cumulative average is required to price the delta.
        ({"previous_filled_qty": Decimal("4"), "filled_quantity": 10}, ValueError),
        (
            {
                "previous_filled_qty": Decimal("4"),
                "previous_average_price": 70000.0,
                "filled_quantity": 10,
            },
            TypeError,
        ),
        (
            {
                "previous_filled_qty": Decimal("4"),
                "previous_average_price": Decimal("0"),
                "filled_quantity": 10,
            },
            ValueError,
        ),
    ],
)
def test_unbookable_inputs_are_refused(overrides, error) -> None:
    with pytest.raises(error):
        _build(**overrides)


def test_raw_payload_records_the_delta_provenance() -> None:
    payload = _build(
        previous_filled_qty=Decimal("4"),
        previous_average_price=Decimal("70000"),
        filled_quantity=10,
        average_price=76000,
    ).raw_payload_json

    assert payload == {
        "source": "kiwoom_mock_reconcile",
        "ledger_id": 7,
        "previous_filled_qty": "4",
        "previous_avg_fill_price": "70000",
        "broker_cumulative_filled_qty": "10",
        "broker_cumulative_avg_price": "76000",
        "delta_fill_price": "80000",
    }

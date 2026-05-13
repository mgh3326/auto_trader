from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.execution_ledger import ExecutionLedger
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.repository import _values_differ


def _fill(**overrides) -> ExecutionLedgerUpsert:  # noqa: ANN003
    data = {
        "broker": "upbit",
        "account_mode": "live",
        "broker_order_id": "order-1",
        "fill_seq": 0,
        "venue": "upbit_krw",
        "instrument_type": "crypto",
        "symbol": "BTC",
        "raw_symbol": "KRW-BTC",
        "side": "buy",
        "filled_qty": Decimal("0.0100000000"),
        "filled_price": Decimal("100000000.0000000000"),
        "filled_notional": Decimal("1000000.0000000000"),
        "fee_amount": Decimal("500.0000000000"),
        "fee_currency": "KRW",
        "filled_at": datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        "currency": "KRW",
        "source": "reconciler",
        "raw_payload_json": {"safe": True},
    }
    data.update(overrides)
    return ExecutionLedgerUpsert(**data)


def _row(fill: ExecutionLedgerUpsert) -> ExecutionLedger:
    return ExecutionLedger(**fill.model_dump())


def test_values_differ_treats_decimal_scale_and_timezone_equivalent() -> None:
    fill = _fill(
        filled_qty=Decimal("0.01"),
        filled_price=Decimal("100000000"),
        filled_notional=Decimal("1000000.0"),
        fee_amount=Decimal("500"),
        filled_at=datetime(2026, 5, 13, 0, 0),  # DB drivers may return naive UTC
    )
    row = _row(_fill(filled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC)))

    assert _values_differ(row, fill) is False


def test_values_differ_detects_changed_fill_price() -> None:
    row = _row(_fill(filled_price=Decimal("100000000")))
    changed = _fill(filled_price=Decimal("100000001"))

    assert _values_differ(row, changed) is True

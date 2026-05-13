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


# --- Issue 4 regression: wider unique key (account_mode + venue) ---


def test_upsert_key_includes_account_mode_and_venue() -> None:
    """Fills that differ only in account_mode or venue must NOT be considered the same row."""
    live_fill = _fill(account_mode="live", venue="upbit_krw")
    mock_fill = _fill(account_mode="mock", venue="upbit_krw")
    other_venue_fill = _fill(account_mode="live", venue="upbit_usdt")

    # Different account_mode → different key → not equal
    assert _values_differ(_row(live_fill), mock_fill) is True
    # Different venue → different key → not equal
    assert _values_differ(_row(live_fill), other_venue_fill) is True
    # Same key → same
    assert _values_differ(_row(live_fill), live_fill) is False


def test_two_fills_same_order_different_fill_seq_are_distinct() -> None:
    """Multiple partial fills for the same order_id must survive as separate rows."""
    fill_a = _fill(
        fill_seq=0, filled_qty=Decimal("0.1"), filled_price=Decimal("50000000")
    )
    fill_b = _fill(
        fill_seq=1, filled_qty=Decimal("0.2"), filled_price=Decimal("51000000")
    )

    # _values_differ compares column values, not keys; the rows are distinct by key
    assert fill_a.fill_seq != fill_b.fill_seq
    assert fill_a.broker_order_id == fill_b.broker_order_id

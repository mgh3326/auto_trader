"""ROB-400: shadow order-history must not contradict lifecycle_state."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.kis_mock_ledger import _shadow_row_to_order


def _row(*, lifecycle_state, quantity, detail):
    return SimpleNamespace(
        id=24,
        order_no=None,
        symbol="0148J0",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=Decimal(str(quantity)),
        price=Decimal("15900"),
        amount=Decimal("159000"),
        currency="KRW",
        trade_date=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        lifecycle_state=lifecycle_state,
        last_reconcile_detail=detail,
    )


@pytest.mark.unit
def test_pending_row_is_unfilled():
    out = _shadow_row_to_order(
        _row(lifecycle_state="pending", quantity=10, detail=None)
    )
    assert out["status"] == "pending"
    assert out["filled_qty"] == 0.0
    assert out["remaining_qty"] == 10.0


@pytest.mark.unit
def test_fill_row_with_full_attribution_reports_filled():
    out = _shadow_row_to_order(
        _row(
            lifecycle_state="fill",
            quantity=10,
            detail={"attributed_fill_qty": "10"},
        )
    )
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0


@pytest.mark.unit
def test_fill_row_with_partial_attribution_reports_partial():
    out = _shadow_row_to_order(
        _row(
            lifecycle_state="fill",
            quantity=10,
            detail={"attributed_fill_qty": "6"},
        )
    )
    assert out["status"] == "partial"
    assert out["filled_qty"] == 6.0
    assert out["remaining_qty"] == 4.0


@pytest.mark.unit
def test_fill_row_without_attribution_falls_back_to_full_fill():
    # legacy/confirm-path fill rows lacking attributed_fill_qty must still not
    # contradict lifecycle="fill" (never status=pending with filled_qty=0).
    out = _shadow_row_to_order(
        _row(lifecycle_state="fill", quantity=10, detail=None)
    )
    assert out["status"] == "filled"
    assert out["filled_qty"] == 10.0
    assert out["remaining_qty"] == 0.0

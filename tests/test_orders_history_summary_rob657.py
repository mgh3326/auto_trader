from __future__ import annotations

import pytest

from app.mcp_server.tooling.orders_history import _calculate_order_summary


@pytest.mark.unit
def test_calculate_order_summary_counts_expired() -> None:
    orders = [
        {"status": "expired"},
        {"status": "pending"},
        {"status": "filled"},
        {"status": "expired"},
    ]

    summary = _calculate_order_summary(orders)

    assert summary["expired"] == 2
    assert summary["pending"] == 1
    assert summary["filled"] == 1
    assert summary["total_orders"] == 4

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.toss_live_evidence import classify_toss_order_evidence

pytestmark = pytest.mark.unit


def _order(status: str, execution: dict | None = None):
    return SimpleNamespace(
        order_id="ord-1",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        time_in_force="DAY",
        status=status,
        price=Decimal("190"),
        quantity=Decimal("2"),
        order_amount=None,
        currency="USD",
        ordered_at="2026-06-12T00:00:00Z",
        canceled_at=None,
        execution=execution or {},
    )


def test_pending_with_zero_fill_is_pending():
    evidence = classify_toss_order_evidence(_order("PENDING"))

    assert evidence.verdict == "pending"
    assert evidence.local_status == "pending"
    assert evidence.filled_qty == Decimal("0")


def test_filled_uses_execution_fee_tax_and_settlement_date():
    evidence = classify_toss_order_evidence(
        _order(
            "FILLED",
            {
                "filledQuantity": Decimal("2"),
                "averageFilledPrice": Decimal("191.25"),
                "commission": Decimal("0.05"),
                "tax": Decimal("0.01"),
                "settlementDate": "2026-06-15",
            },
        )
    )

    assert evidence.verdict == "filled"
    assert evidence.local_status == "filled"
    assert evidence.filled_qty == Decimal("2")
    assert evidence.avg_price == Decimal("191.25")
    assert evidence.fee_total == Decimal("0.06")
    assert evidence.settlement_date.isoformat() == "2026-06-15"


def test_cancelled_partial_books_delta_then_terminal_cancelled():
    evidence = classify_toss_order_evidence(
        _order(
            "CANCELED",
            {
                "filledQuantity": Decimal("0.5"),
                "averageFilledPrice": Decimal("190.5"),
                "commission": Decimal("0.02"),
                "tax": Decimal("0"),
            },
        )
    )

    assert evidence.verdict == "partial"
    assert evidence.local_status == "cancelled"
    assert evidence.filled_qty == Decimal("0.5")


def test_replaced_with_fill_books_then_terminal_replaced():
    evidence = classify_toss_order_evidence(
        _order(
            "REPLACED",
            {
                "filledQuantity": Decimal("1"),
                "averageFilledPrice": Decimal("190.5"),
            },
        )
    )

    assert evidence.verdict == "partial"
    assert evidence.local_status == "replaced"


def test_cancel_rejected_keeps_original_open_semantics():
    evidence = classify_toss_order_evidence(_order("CANCEL_REJECTED"))

    assert evidence.verdict == "pending"
    assert evidence.local_status == "cancel_rejected"


@pytest.mark.asyncio
async def test_adapter_fetches_single_order_detail():
    from app.mcp_server.tooling import toss_live_evidence as ev

    class _Row:
        broker_order_id = "ord-1"

    client = SimpleNamespace(
        get_order=AsyncMock(
            return_value=_order(
                "FILLED",
                {"filledQuantity": Decimal("1"), "averageFilledPrice": Decimal("10")},
            )
        ),
        aclose=AsyncMock(),
    )

    with patch.object(ev.TossReadClient, "from_settings", return_value=client):
        evidence = await ev.TossEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == "filled"
    client.get_order.assert_awaited_once_with("ord-1")
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_adapter_reuses_injected_client_without_closing():
    from app.mcp_server.tooling import toss_live_evidence as ev

    class _Row:
        broker_order_id = "ord-9"

    injected = SimpleNamespace(
        get_order=AsyncMock(
            return_value=_order(
                "FILLED",
                {"filledQuantity": Decimal("1"), "averageFilledPrice": Decimal("10")},
            )
        ),
        aclose=AsyncMock(),
    )

    # from_settings must NOT be called when a client is injected.
    with patch.object(
        ev.TossReadClient, "from_settings", side_effect=AssertionError("newed a client")
    ):
        evidence = await ev.TossEvidenceAdapter(client=injected).fetch_evidence(_Row())

    assert evidence.verdict == "filled"
    injected.get_order.assert_awaited_once_with("ord-9")
    injected.aclose.assert_not_awaited()  # caller owns the shared client

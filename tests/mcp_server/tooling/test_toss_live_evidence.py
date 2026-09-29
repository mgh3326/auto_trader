from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.mcp_server.tooling.toss_live_evidence import classify_toss_order_evidence

pytestmark = pytest.mark.unit


def _order(
    status: str,
    execution: dict | None = None,
    *,
    time_in_force: str = "DAY",
    canceled_at: str | None = None,
):
    return SimpleNamespace(
        order_id="ord-1",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        time_in_force=time_in_force,
        status=status,
        price=Decimal("190"),
        quantity=Decimal("2"),
        order_amount=None,
        currency="USD",
        ordered_at="2026-06-12T00:00:00Z",
        canceled_at=canceled_at,
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


# ---- ROB-691 — Toss DAY-expiry sweep classification ----
# Toss reports a broker-swept KR DAY order as REJECTED + canceledAt at the
# session sweep (~15:33 KST regular, ~20:04 KST NXT after-hours).  Expiry is
# classified only from that broker evidence; anything weaker stays non-expired.


def test_day_sweep_regular_session_1533_is_expired():
    evidence = classify_toss_order_evidence(
        _order("REJECTED", canceled_at="2026-09-24T15:33:12.123456+09:00")
    )

    assert evidence.verdict == "expired"
    assert evidence.local_status == "expired"
    assert evidence.filled_qty == Decimal("0")
    assert evidence.expired_at == datetime(
        2026, 9, 24, 15, 33, 12, 123456, tzinfo=ZoneInfo("Asia/Seoul")
    )


def test_day_sweep_nxt_after_hours_2004_is_expired():
    evidence = classify_toss_order_evidence(
        _order("REJECTED", canceled_at="2026-09-24T20:04:31.000000+09:00")
    )

    assert evidence.verdict == "expired"
    assert evidence.local_status == "expired"
    assert evidence.expired_at == datetime(
        2026, 9, 24, 20, 4, 31, tzinfo=ZoneInfo("Asia/Seoul")
    )


def test_rejected_without_canceled_at_stays_rejected_not_expired():
    """Submit-time rejection has no sweep timestamp — never expiry."""
    evidence = classify_toss_order_evidence(_order("REJECTED"))

    assert evidence.verdict == "none"
    assert evidence.local_status == "rejected"
    assert evidence.expired_at is None


def test_rejected_day_with_unparseable_canceled_at_stays_rejected():
    """An unreadable broker timestamp is not expiry evidence (fail-closed)."""
    evidence = classify_toss_order_evidence(
        _order("REJECTED", canceled_at="not-a-timestamp")
    )

    assert evidence.local_status == "rejected"
    assert evidence.expired_at is None


def test_non_day_rejected_with_canceled_at_is_not_expired():
    evidence = classify_toss_order_evidence(
        _order("REJECTED", time_in_force="GTD", canceled_at="2026-09-24T15:33:12+09:00")
    )

    assert evidence.local_status == "rejected"
    assert evidence.expired_at is None


def test_canceled_day_with_canceled_at_is_cancelled_not_expired():
    """An operator/broker CANCELED also carries canceledAt — it is a cancel,
    not a DAY sweep.  Only REJECTED+canceledAt classifies expiry."""
    evidence = classify_toss_order_evidence(
        _order("CANCELED", canceled_at="2026-09-24T15:33:12+09:00")
    )

    assert evidence.verdict == "none"
    assert evidence.local_status == "cancelled"
    assert evidence.expired_at is None


def test_partial_fill_then_day_sweep_preserves_fill_and_expires():
    """Mutant guard: a swept order with fills is partial+expired — the booked
    quantity must survive.  Classifying it as unfilled expired goes RED."""
    evidence = classify_toss_order_evidence(
        _order(
            "REJECTED",
            {
                "filledQuantity": Decimal("0.5"),
                "averageFilledPrice": Decimal("190.5"),
            },
            canceled_at="2026-09-24T15:33:12+09:00",
        )
    )

    assert evidence.verdict == "partial"
    assert evidence.local_status == "expired"
    assert evidence.filled_qty == Decimal("0.5")
    assert evidence.expired_at == datetime(
        2026, 9, 24, 15, 33, 12, tzinfo=ZoneInfo("Asia/Seoul")
    )


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

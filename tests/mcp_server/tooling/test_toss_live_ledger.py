from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    # Create a mock that when called twice returns db_session
    # async with _order_session_factory()() as db:
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None

    def factory_call():
        return mock_cm

    with patch.object(toss_live_ledger, "_order_session_factory", return_value=factory_call):
        yield


async def _accepted(db_session, *, side: str = "buy"):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side=side,
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id=f"cid-{side}",
        broker_order_id=f"ord-{side}",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t" if side == "buy" else None,
        strategy="s" if side == "buy" else None,
        exit_reason="trim" if side == "sell" else None,
    )


async def test_reconcile_filled_buy_books_once(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out1 = await mod._reconcile_one_toss_row(row, dry_run=False)
        row2 = await db_session.get(TossLiveOrderLedger, row.id)
        db_session.expunge(row2)
        out2 = await mod._reconcile_one_toss_row(row2, dry_run=False)

    assert out1["action"] == "booked"
    assert out2["action"] == "noop_already_booked"
    assert m_fill.await_count == 1
    assert m_fill.await_args.kwargs["fee"] == 0.06
    assert m_journal.await_count == 1


async def test_reconcile_cancelled_partial_books_delta_and_terminal(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="cancelled",
        broker_status="CANCELED",
        filled_qty=Decimal("0.5"),
        avg_price=Decimal("190.5"),
        commission=Decimal("0.02"),
        tax=Decimal("0"),
        fee_total=Decimal("0.02"),
        settlement_date=None,
        raw_order={"status": "CANCELED"},
        reason="partial cancelled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=303)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 404}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "cancelled"
    assert refreshed.filled_qty == Decimal("0.5")


async def test_reconcile_pending_is_noop(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="pending",
        local_status="pending",
        broker_status="PENDING",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "PENDING"},
        reason="pending",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"


async def test_reconcile_impl_lists_only_toss_rows(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session)

    with patch.object(
        mod,
        "_reconcile_one_toss_row",
        new=AsyncMock(return_value={"verdict": "pending", "action": "noop_pending"}),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {"pending": 1}


async def test_rejected_replacement_reopens_original_order(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    original = await _accepted(db_session)
    replacement = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="modify",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("191"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-rejected-replacement",
        broker_order_id="ord-rejected-replacement",
        original_order_id=original.broker_order_id,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    original.replaced_by_order_id = replacement.broker_order_id
    await db_session.commit()
    db_session.expunge(replacement)

    evidence = TossFillEvidence(
        verdict="pending",
        local_status="replace_rejected",
        broker_status="REPLACE_REJECTED",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "REPLACE_REJECTED"},
        reason="replace rejected",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(replacement, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed_original = await db_session.get(TossLiveOrderLedger, original.id)
    refreshed_replacement = await db_session.get(TossLiveOrderLedger, replacement.id)
    assert refreshed_original.status == "accepted"
    assert refreshed_original.replaced_by_order_id is None
    assert refreshed_replacement.status == "replace_rejected"

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.brokers.toss.dto import TossOrder
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
pytestmark.append(pytest.mark.usefixtures("toss_ledger_cleanup_lock"))


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session, toss_ledger_cleanup_lock):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None
    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=lambda: mock_cm
    ):
        yield


async def _accepted(db_session, *, cid: str, oid: str):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="kr",
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3"),
        price=Decimal("85000"),
        order_amount=None,
        currency="KRW",
        client_order_id=cid,
        broker_order_id=oid,
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t",
        strategy="s",
    )


def _pending_order(order_id: str) -> TossOrder:
    return TossOrder(
        order_id=order_id,
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        status="PENDING",
        price=Decimal("85000"),
        quantity=Decimal("3"),
        order_amount=None,
        currency="KRW",
        ordered_at="2026-07-01T00:00:00Z",
        canceled_at=None,
        execution={"filledQuantity": Decimal("0")},
    )


async def test_run_uses_one_shared_client_reused_by_batch_and_fallback(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session, cid="c1", oid="o1")
    await _accepted(db_session, cid="c2", oid="o2")

    # A single fake client. list_orders raises so batch build fails and the run
    # MUST fall back per-row AND reuse this exact client (never new one per row).
    fake_client = SimpleNamespace(
        list_orders=AsyncMock(side_effect=RuntimeError("force-build-fail")),
        get_order=AsyncMock(side_effect=lambda oid: _pending_order(oid)),
        aclose=AsyncMock(),
    )
    from_settings = MagicMock(return_value=fake_client)

    with patch.object(mod.TossReadClient, "from_settings", from_settings):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert from_settings.call_count == 1  # ONE client for the whole run
    assert fake_client.get_order.await_count == 2  # both rows via the shared client
    fake_client.aclose.assert_awaited_once()  # closed exactly once
    assert out["counts"] == {"pending": 2}

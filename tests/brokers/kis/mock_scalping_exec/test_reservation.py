"""ROB-843 P1 — write-ahead reservation lifecycle for KIS mock scalping entries.

A durable reservation is recorded BEFORE the broker POST. If the durable write
fails the POST never happens; the reservation is released only when the order is
confirmed fully tracked or proven not sent, and an unresolved reservation is the
restart-safe fail-close signal.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory
from app.mcp_server.tooling.order_execution import OrderSendOutcomeUnknown
from app.models.review import OrderSendIntent
from app.services.brokers.kis.mock_scalping_exec import adapters
from app.services.brokers.kis.mock_scalping_exec.reservation import (
    has_unresolved_entries,
    reserve_entry,
)
from app.services.order_send_intent_service import KIS_MOCK_SCALPING_SCOPE


@pytest_asyncio.fixture(autouse=True)
async def _clear_reservations():
    async def _c():
        async with _order_session_factory()() as db:
            await db.execute(
                delete(OrderSendIntent).where(
                    OrderSendIntent.account_scope == KIS_MOCK_SCALPING_SCOPE
                )
            )
            await db.commit()

    await _c()
    yield
    await _c()


def _broker():
    b = adapters.KisMockBroker(get_state=lambda _s: None)
    # confirm=True normally reads the live mock balance; stub it for tests.
    b._capture_baseline = AsyncMock(return_value={"symbol": "005930"})  # type: ignore[method-assign]
    return b


async def _submit(broker, cid: str):
    return await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id=cid,
        confirm=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserve_failure_blocks_post(monkeypatch) -> None:
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)
    monkeypatch.setattr(
        adapters, "reserve_entry", AsyncMock(side_effect=RuntimeError("db down"))
    )
    result = await _submit(_broker(), "cid-reserve-fail")
    assert result["reservation_blocked"] is True
    assert "reservation_unavailable" in result["reason_codes"]
    assert place.await_count == 0  # POST 0 — durable write failed first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_reservation_blocks_post(monkeypatch) -> None:
    place = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapters, "_place_order_impl", place)
    cid = "cid-dup"
    await reserve_entry(correlation_id=cid, symbol="005930", side="buy")
    result = await _submit(_broker(), cid)
    assert result["reservation_blocked"] is True
    assert "duplicate_send" in result["reason_codes"]
    assert place.await_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_success_releases_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters, "_place_order_impl", AsyncMock(return_value={"success": True})
    )
    await _submit(_broker(), "cid-ok")
    assert await has_unresolved_entries() is False  # released — fully tracked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_lost_keeps_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(return_value={"success": True, "ledger_tracking_unavailable": True}),
    )
    await _submit(_broker(), "cid-native-lost")
    assert await has_unresolved_entries() is True  # kept — uncertain/lost


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uncertain_send_keeps_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(side_effect=OrderSendOutcomeUnknown(TimeoutError("t"))),
    )
    with pytest.raises(OrderSendOutcomeUnknown):
        await _submit(_broker(), "cid-uncertain")
    assert await has_unresolved_entries() is True  # kept — outcome unknown


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deterministic_rejection_releases_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(side_effect=RuntimeError("40 rejected")),
    )
    with pytest.raises(RuntimeError):
        await _submit(_broker(), "cid-rejected")
    assert await has_unresolved_entries() is False  # released — no order created


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pre_send_block_releases_reservation(monkeypatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_place_order_impl",
        AsyncMock(return_value={"success": False, "pre_send_blocked": True}),
    )
    await _submit(_broker(), "cid-presend")
    assert await has_unresolved_entries() is False  # released — POST 0

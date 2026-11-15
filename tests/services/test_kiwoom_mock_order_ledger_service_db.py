"""hk 4712 (3797 T-P2) — Kiwoom mock order ledger service against the test DB.

CI only (``integration``): never run locally. Rows are keyed by a per-test random
``client_order_id`` and removed in ``finally``; the shared schema is left as
bootstrapped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.review import KiwoomMockOrderLedger, OrderSendIntent
from app.services.kiwoom_mock_order_ledger_service import (
    RESERVATION_ACCOUNT_SCOPE,
    DuplicateOrderIntent,
    KiwoomMockLedgerPayloadConflict,
    KiwoomMockOrderLedgerService,
    KiwoomMockPlaceRecorder,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _kwargs(client_order_id: str, **overrides) -> dict:
    kwargs = {
        "status": "accepted",
        "client_order_id": client_order_id,
        "idempotency_key": client_order_id,
        "symbol": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "broker_order_id": f"T{uuid.uuid4().hex[:12]}",
        "response_code": "0",
        "response_message": "정상",
        "raw_response": {"return_code": 0},
        "trading_day": "20261116",
    }
    kwargs.update(overrides)
    return kwargs


async def _cleanup(client_order_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(KiwoomMockOrderLedger).where(
                KiwoomMockOrderLedger.client_order_id == client_order_id
            )
        )
        await session.execute(
            delete(OrderSendIntent).where(
                OrderSendIntent.account_scope == RESERVATION_ACCOUNT_SCOPE,
                OrderSendIntent.idempotency_key == client_order_id,
            )
        )
        await session.commit()


async def _count(client_order_id: str) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(KiwoomMockOrderLedger)
                .where(KiwoomMockOrderLedger.client_order_id == client_order_id)
            )
            or 0
        )


async def test_record_send_persists_and_is_idempotent(db_session):
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    kwargs = _kwargs(key)
    try:
        first = await KiwoomMockOrderLedgerService(db_session).record_send(**kwargs)
        async with AsyncSessionLocal() as other:
            second = await KiwoomMockOrderLedgerService(other).record_send(**kwargs)
            second_id = second.id

        assert second_id == first.id
        assert await _count(key) == 1
        assert first.status == "accepted"
        assert first.account_mode == "kiwoom_mock"
        assert first.amount == Decimal("210000")
    finally:
        await _cleanup(key)


async def test_same_key_with_a_different_payload_is_a_conflict(db_session):
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    try:
        await KiwoomMockOrderLedgerService(db_session).record_send(**_kwargs(key))
        with pytest.raises(KiwoomMockLedgerPayloadConflict):
            await KiwoomMockOrderLedgerService(db_session).record_send(
                **_kwargs(key, price=70100)
            )
        assert await _count(key) == 1
    finally:
        await _cleanup(key)


async def test_uncertain_row_has_no_broker_order_id_and_replays(db_session):
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    kwargs = _kwargs(key, broker_order_id=None, response_code=None)
    kwargs.pop("status")
    try:
        service = KiwoomMockOrderLedgerService(db_session)
        first = await service.record_uncertain(**kwargs)
        again = await service.record_uncertain(**kwargs)

        assert first.status == "uncertain"
        assert first.broker_order_id is None
        assert again.id == first.id
        assert await _count(key) == 1
    finally:
        await _cleanup(key)


async def test_accepted_without_broker_order_id_is_refused_by_the_check(db_session):
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    try:
        # Service path refuses before the database is touched.
        with pytest.raises(ValueError):
            await KiwoomMockOrderLedgerService(db_session).record_send(
                **_kwargs(key, broker_order_id=None)
            )
        assert await _count(key) == 0

        # And the CHECK still refuses a row that bypasses the service.
        db_session.add(
            KiwoomMockOrderLedger(
                trade_date=datetime.now(UTC),
                trading_day="20261116",
                symbol="005930",
                raw_symbol="005930",
                instrument_type="equity_kr",
                market="kr",
                side="buy",
                order_type="limit",
                operation_kind="place",
                quantity=Decimal("1"),
                price=Decimal("70000"),
                currency="KRW",
                client_order_id=key,
                idempotency_key=key,
                account_mode="kiwoom_mock",
                broker="kiwoom",
                status="accepted",
                broker_order_id=None,
            )
        )
        with pytest.raises(IntegrityError) as excinfo:
            await db_session.flush()
        await db_session.rollback()
        assert "ck_kiwoom_mock_order_ledger_accepted_has_broker_order_id" in str(
            excinfo.value
        )
    finally:
        await _cleanup(key)


async def test_recorder_reservation_blocks_a_duplicate_and_release_reopens_it():
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    recorder = KiwoomMockPlaceRecorder(AsyncSessionLocal)
    try:
        reservation = await recorder.reserve(
            idempotency_key=key, symbol="005930", side="buy"
        )
        with pytest.raises(DuplicateOrderIntent):
            await recorder.reserve(idempotency_key=key, symbol="005930", side="buy")

        assert await recorder.release(reservation) == 1
        again = await recorder.reserve(idempotency_key=key, symbol="005930", side="buy")
        assert again.row_id != reservation.row_id
    finally:
        await _cleanup(key)


async def test_recorder_returns_a_detached_receipt():
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    recorder = KiwoomMockPlaceRecorder(AsyncSessionLocal)
    try:
        receipt = await recorder.record_send(**_kwargs(key))

        assert receipt.status == "accepted"
        assert receipt.client_order_id == key
        assert receipt.ledger_id > 0
    finally:
        await _cleanup(key)


async def test_recorder_existing_status_reads_without_writing():
    key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    recorder = KiwoomMockPlaceRecorder(AsyncSessionLocal)
    try:
        assert await recorder.existing_status(key) is None
        await recorder.record_send(
            **_kwargs(key, status="rejected", broker_order_id=None)
        )

        assert await recorder.existing_status(key) == "rejected"
        assert await _count(key) == 1
    finally:
        await _cleanup(key)

"""hk 4754 (3797 T-P8) — US Kiwoom mock ledger rows against the test DB.

CI only (``integration``): never run locally. Rows are keyed by a per-test random
``client_order_id`` and removed in ``finally``; the shared schema is left as
bootstrapped. The US account shares the table with KR, so the DB CHECK that
binds ``account_mode``/``market``/``instrument_type``/``currency`` is the thing
under test here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.core.db import AsyncSessionLocal, engine
from app.models.review import KiwoomMockOrderLedger, OrderSendIntent
from app.services.kiwoom_mock_order_ledger_service import (
    ACCOUNT_MODE_KIWOOM_MOCK_US,
    RESERVATION_ACCOUNT_SCOPE_US,
    KiwoomMockOrderLedgerService,
    KiwoomMockPlaceRecorder,
    KiwoomMockTransitionRefused,
    us_trading_day,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _us_kwargs(client_order_id: str, **overrides) -> dict:
    kwargs = {
        "status": "accepted",
        "client_order_id": client_order_id,
        "idempotency_key": client_order_id,
        "symbol": "NVDA",
        "side": "buy",
        "quantity": 2,
        "price": Decimal("213.04"),
        "broker_order_id": "000000063",
        "response_code": "0",
        "response_message": "모의투자 매수주문완료",
        "raw_response": {"return_code": 0, "ord_no": "000000063"},
        "trading_day": us_trading_day(datetime.now(UTC)),
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
    }
    kwargs.update(overrides)
    return kwargs


def _kr_kwargs(client_order_id: str) -> dict:
    return {
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


async def _cleanup(*client_order_ids: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(KiwoomMockOrderLedger).where(
                KiwoomMockOrderLedger.client_order_id.in_(list(client_order_ids))
            )
        )
        await session.execute(
            delete(OrderSendIntent).where(
                OrderSendIntent.account_scope.in_(
                    ["kiwoom_mock", RESERVATION_ACCOUNT_SCOPE_US]
                ),
                OrderSendIntent.idempotency_key.in_(list(client_order_ids)),
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


async def test_us_record_send_persists_the_us_identity() -> None:
    key = f"kwoomus-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            row = await KiwoomMockOrderLedgerService(session).record_send(
                **_us_kwargs(key)
            )

        assert row.account_mode == ACCOUNT_MODE_KIWOOM_MOCK_US
        assert row.market == "us"
        assert row.instrument_type == "equity_us"
        assert row.currency == "USD"
        assert row.order_type == "limit"
        assert row.broker == "kiwoom"
        assert row.amount == Decimal("426.08")
        assert row.trading_day == us_trading_day(datetime.now(UTC))
        assert await _count(key) == 1
    finally:
        await _cleanup(key)


async def test_us_market_order_records_without_a_price() -> None:
    key = f"kwoomus-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            row = await KiwoomMockOrderLedgerService(session).record_send(
                **_us_kwargs(key, price=None, order_type="market")
            )

        assert row.order_type == "market"
        assert row.price is None
        assert row.amount is None
    finally:
        await _cleanup(key)


async def test_us_recorder_reserves_under_the_us_account_scope() -> None:
    key = f"kwoomus-{uuid.uuid4().hex[:16]}"
    recorder = KiwoomMockPlaceRecorder(
        AsyncSessionLocal, account_mode=ACCOUNT_MODE_KIWOOM_MOCK_US
    )
    try:
        await recorder.reserve(idempotency_key=key, symbol="NVDA", side="buy")

        async with AsyncSessionLocal() as session:
            scopes = (
                (
                    await session.execute(
                        select(OrderSendIntent.account_scope).where(
                            OrderSendIntent.idempotency_key == key
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert scopes == [RESERVATION_ACCOUNT_SCOPE_US]
    finally:
        await _cleanup(key)


async def test_reconcile_scans_are_scoped_by_account_mode() -> None:
    us_key = f"kwoomus-{uuid.uuid4().hex[:16]}"
    kr_key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            await service.record_send(**_us_kwargs(us_key))
            await service.record_send(**_kr_kwargs(kr_key))

            us_rows, _total = await service.list_reconcile_candidates(
                limit=500, account_mode=ACCOUNT_MODE_KIWOOM_MOCK_US
            )
            kr_rows, _total = await service.list_reconcile_candidates(limit=500)

        us_ids = {row.client_order_id for row in us_rows}
        kr_ids = {row.client_order_id for row in kr_rows}
        assert us_key in us_ids and kr_key not in us_ids
        assert kr_key in kr_ids and us_key not in kr_ids
    finally:
        await _cleanup(us_key, kr_key)


async def test_us_transition_refuses_a_kr_row() -> None:
    kr_key = f"kwoomm-{uuid.uuid4().hex[:16]}"
    try:
        async with AsyncSessionLocal() as session:
            service = KiwoomMockOrderLedgerService(session)
            row = await service.record_send(**_kr_kwargs(kr_key))
            with pytest.raises(KiwoomMockTransitionRefused) as excinfo:
                await service.mark_terminal(
                    ledger_id=row.id,
                    status="cancelled",
                    reason="unit",
                    account_mode=ACCOUNT_MODE_KIWOOM_MOCK_US,
                )

        assert excinfo.value.reason == "row_not_found"
    finally:
        await _cleanup(kr_key)

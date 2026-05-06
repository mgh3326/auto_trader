from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.pending_order import PendingOrder
from app.services.pending_order_sync_service import PendingOrderSyncService


@pytest_asyncio.fixture
async def db_session():
    from app.models.trading import User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        u = User(
            id=1,
            email="test@example.com",
            username="testuser",
            created_at=datetime.now(UTC),
        )
        session.add(u)
        await session.commit()
        yield session


class DummyBroker:
    def __init__(self, orders=None, exc: Exception | None = None):
        self.orders = orders or []
        self.exc = exc

    async def fetch_open_orders(self):
        if self.exc is not None:
            raise self.exc
        return self.orders


def _open_order_payload() -> dict:
    return {
        "broker_order_id": "ORD-1",
        "symbol": "BTC",
        "market": "crypto",
        "side": "buy",
        "order_type": "limit",
        "price": "50000.0",
        "quantity": "0.1",
        "filled_quantity": "0.0",
        "status": "open",
        "ordered_at": datetime.now(UTC),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_order_sync_upserts_correctly(db_session):
    # Ensure pending_orders table exists in SQLite
    async with db_session.bind.begin() as conn:
        await conn.run_sync(PendingOrder.__table__.create)

    service = PendingOrderSyncService(db_session)

    broker = DummyBroker([_open_order_payload()])

    results = await service.sync_all_venues(user_id=1, venues={"upbit": broker})
    assert results["upbit"] == 1

    # Verify persistence
    stmt = select(PendingOrder).where(PendingOrder.broker_order_id == "ORD-1")
    order = (await db_session.execute(stmt)).scalar_one()
    assert order.symbol == "BTC"
    assert order.quantity == Decimal("0.1")

    # Sync again with update
    broker.orders[0]["status"] = "partial_fill"
    broker.orders[0]["filled_quantity"] = "0.05"

    await service.sync_all_venues(user_id=1, venues={"upbit": broker})

    await db_session.refresh(order)
    assert order.status == "partial_fill"
    assert order.filled_quantity == Decimal("0.05")

    # Sync with deletion after a successful complete empty snapshot.
    broker.orders = []
    await service.sync_all_venues(user_id=1, venues={"upbit": broker})

    order_after = (await db_session.execute(stmt)).scalar_one_or_none()
    assert order_after is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_order_sync_preserves_rows_when_adapter_fails(db_session):
    async with db_session.bind.begin() as conn:
        await conn.run_sync(PendingOrder.__table__.create)

    service = PendingOrderSyncService(db_session)
    broker = DummyBroker([_open_order_payload()])
    await service.sync_all_venues(user_id=1, venues={"upbit": broker})

    broker.exc = NotImplementedError("placeholder adapter unsupported")
    broker.orders = []

    results = await service.sync_all_venues(user_id=1, venues={"upbit": broker})
    assert results["upbit"] == -1

    stmt = select(PendingOrder).where(PendingOrder.broker_order_id == "ORD-1")
    order_after = (await db_session.execute(stmt)).scalar_one_or_none()
    assert order_after is not None

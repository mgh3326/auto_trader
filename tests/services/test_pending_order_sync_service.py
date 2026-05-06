import pytest
import pytest_asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from app.models.base import Base
from app.services.pending_order_sync_service import PendingOrderSyncService
from app.models.pending_order import PendingOrder
from sqlalchemy import select

@pytest_asyncio.fixture
async def db_session():
    from app.models.trading import User
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        u = User(id=1, email="test@example.com", username="testuser", created_at=datetime.now(timezone.utc))
        session.add(u)
        await session.commit()
        yield session

@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_order_sync_upserts_correctly(db_session):
    # Ensure pending_orders table exists in SQLite
    async with db_session.bind.begin() as conn:
        await conn.run_sync(PendingOrder.__table__.create)

    service = PendingOrderSyncService(db_session)
    
    mock_broker = AsyncMock()
    mock_broker.fetch_open_orders.return_value = [
        {
            "broker_order_id": "ORD-1",
            "symbol": "BTC",
            "market": "crypto",
            "side": "buy",
            "order_type": "limit",
            "price": "50000.0",
            "quantity": "0.1",
            "filled_quantity": "0.0",
            "status": "open",
            "ordered_at": datetime.now(timezone.utc),
        }
    ]
    
    results = await service.sync_all_venues(user_id=1, venues={"upbit": mock_broker})
    assert results["upbit"] == 1
    
    # Verify persistence
    stmt = select(PendingOrder).where(PendingOrder.broker_order_id == "ORD-1")
    order = (await db_session.execute(stmt)).scalar_one()
    assert order.symbol == "BTC"
    assert order.quantity == Decimal("0.1")
    
    # Sync again with update
    mock_broker.fetch_open_orders.return_value[0]["status"] = "partial_fill"
    mock_broker.fetch_open_orders.return_value[0]["filled_quantity"] = "0.05"
    
    await service.sync_all_venues(user_id=1, venues={"upbit": mock_broker})
    
    await db_session.refresh(order)
    assert order.status == "partial_fill"
    assert order.filled_quantity == Decimal("0.05")
    
    # Sync with deletion
    mock_broker.fetch_open_orders.return_value = []
    await service.sync_all_venues(user_id=1, venues={"upbit": mock_broker})
    
    order_after = (await db_session.execute(stmt)).scalar_one_or_none()
    assert order_after is None

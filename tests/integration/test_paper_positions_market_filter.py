"""Integration test: PaperTradingService.get_positions() market filtering."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.paper_trading import PaperAccount, PaperPosition
from app.models.trading import InstrumentType
from app.services.paper_trading_service import PaperTradingService


@pytest_asyncio.fixture
async def async_db():
    """In-memory SQLite async session with schema translation.

    Paper trading models use ``schema="paper"`` (PostgreSQL). SQLite has no
    schema support, so we translate ``"paper" -> None`` via
    ``schema_translate_map`` at both DDL and DML time.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        execution_options={"schema_translate_map": {"paper": None}},
    )
    from sqlalchemy import Integer

    for table in [PaperAccount.__table__, PaperPosition.__table__]:
        for column in table.columns:
            if column.primary_key:
                column.type = Integer()

    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                PaperAccount.__table__,
                PaperPosition.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_positions(db: AsyncSession) -> int:
    """Insert one account with three positions (kr, us, crypto). Return account id."""
    account = PaperAccount(
        name="test",
        initial_capital=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    db.add(account)
    await db.flush()

    for symbol, itype in [
        ("005930", InstrumentType.equity_kr),
        ("AAPL", InstrumentType.equity_us),
        ("KRW-BTC", InstrumentType.crypto),
    ]:
        db.add(
            PaperPosition(
                account_id=account.id,
                symbol=symbol,
                instrument_type=itype,
                quantity=Decimal("1"),
                avg_price=Decimal("100"),
                total_invested=Decimal("100"),
            )
        )
    await db.flush()
    return account.id


@pytest.mark.asyncio
async def test_get_positions_no_market_returns_all(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(
        service, "_fetch_current_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id)

    assert len(positions) == 3


@pytest.mark.asyncio
async def test_get_positions_market_equity_kr(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(
        service, "_fetch_current_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="equity_kr")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "005930"
    assert positions[0]["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_get_positions_market_equity_us(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(
        service, "_fetch_current_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="equity_us")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_positions_market_crypto(async_db: AsyncSession):
    account_id = await _seed_positions(async_db)
    service = PaperTradingService(async_db)

    with patch.object(
        service, "_fetch_current_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.side_effect = Exception("skip price")
        positions = await service.get_positions(account_id, market="crypto")

    assert len(positions) == 1
    assert positions[0]["symbol"] == "KRW-BTC"

# tests/test_invest_screener_snapshots_model.py
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.models.invest_screener_snapshot import InvestScreenerSnapshot


@pytest_asyncio.fixture(autouse=True)
async def _clean_snapshots(db_session):
    await db_session.execute(delete(InvestScreenerSnapshot))
    await db_session.commit()
    yield


@pytest.mark.asyncio
async def test_insert_round_trip(db_session):
    snap = InvestScreenerSnapshot(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        prev_close=Decimal("77900"),
        change_amount=Decimal("600"),
        change_rate=Decimal("0.7702"),
        consecutive_up_days=3,
        week_change_rate=Decimal("2.1500"),
        closes_window=[77000, 77400, 77900, 78500],
        daily_volume=14_500_000,
        source="kis",
    )
    db_session.add(snap)
    await db_session.commit()

    result = await db_session.execute(
        sa.select(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol == "005930"
        )
    )
    fetched = result.scalar_one()
    assert fetched.consecutive_up_days == 3
    assert fetched.closes_window == [77000, 77400, 77900, 78500]


@pytest.mark.asyncio
async def test_unique_constraint(db_session):
    base = dict(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        closes_window=[78500],
        source="kis",
    )
    db_session.add(InvestScreenerSnapshot(**base))
    await db_session.commit()

    db_session.add(InvestScreenerSnapshot(**base))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_market_check_constraint(db_session):
    db_session.add(
        InvestScreenerSnapshot(
            market="crypto",  # invalid
            symbol="BTC",
            snapshot_date=dt.date(2026, 5, 9),
            latest_close=Decimal("100000"),
            closes_window=[100000],
            source="kis",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()

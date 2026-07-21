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
    test_symbols = {"910001", "910002", "BTC"}
    await db_session.execute(
        delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol.in_(test_symbols)
        )
    )
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(
        delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol.in_(test_symbols)
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_insert_round_trip(db_session):
    snap = InvestScreenerSnapshot(
        market="kr",
        symbol="910001",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        prev_close=Decimal("77900"),
        change_amount=Decimal("600"),
        change_rate=Decimal("0.7702"),
        consecutive_up_days=3,
        week_change_rate=Decimal("2.1500"),
        closes_window=[77000, 77400, 77900, 78500],
        daily_volume=14_500_000,
        daily_turnover=Decimal("1138250000000"),
        market_cap=Decimal("400000000000000"),
        market_cap_source="naver_finance",
        market_cap_snapshot_date=dt.date(2026, 5, 9),
        support_price=Decimal("77000"),
        support_kind="bb_lower,fib_0.618",
        support_strength="strong",
        dist_to_support_pct=Decimal("1.9108"),
        support_computed_at=dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.UTC),
        source="kis",
    )
    db_session.add(snap)
    await db_session.commit()

    result = await db_session.execute(
        sa.select(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.symbol == "910001"
        )
    )
    fetched = result.scalar_one()
    assert fetched.consecutive_up_days == 3
    assert fetched.closes_window == [77000, 77400, 77900, 78500]
    assert fetched.market_cap == Decimal("400000000000000")
    assert fetched.support_price == Decimal("77000")
    assert fetched.dist_to_support_pct == Decimal("1.9108")
    assert fetched.support_computed_at == dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_unique_constraint(db_session):
    base = {
        "market": "kr",
        "symbol": "910002",
        "snapshot_date": dt.date(2026, 5, 9),
        "latest_close": Decimal("78500"),
        "closes_window": [78500],
        "source": "kis",
    }
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

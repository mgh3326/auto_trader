import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    payload = SnapshotUpsert(
        market="kr",
        symbol="005930",
        snapshot_date=dt.date(2026, 5, 9),
        latest_close=Decimal("78500"),
        prev_close=Decimal("77900"),
        change_amount=Decimal("600"),
        change_rate=Decimal("0.7702"),
        consecutive_up_days=3,
        week_change_rate=Decimal("2.15"),
        closes_window=[77000, 77400, 77900, 78500],
        daily_volume=14_500_000,
        source="kis",
    )
    await repo.upsert(payload)
    await db_session.commit()

    payload2 = payload.model_copy(update={"consecutive_up_days": 4})
    await repo.upsert(payload2)
    await db_session.commit()

    rows = await repo.get_fresh(market="kr", symbols=["005930"], on_or_after=dt.date(2026, 5, 9))
    assert len(rows) == 1
    assert rows[0].consecutive_up_days == 4


@pytest.mark.asyncio
async def test_get_fresh_filters_stale(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    # Use a symbol distinct from other tests to avoid cross-test contamination.
    await repo.upsert(SnapshotUpsert(
        market="kr", symbol="T_STALE_001", snapshot_date=dt.date(2026, 5, 1),
        latest_close=Decimal("70000"), closes_window=[70000], source="kis",
    ))
    await db_session.commit()

    rows = await repo.get_fresh(market="kr", symbols=["T_STALE_001"], on_or_after=dt.date(2026, 5, 9))
    assert rows == []


@pytest.mark.asyncio
async def test_coverage_counts(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    today = dt.date(2026, 5, 9)
    # Use symbols distinct from other tests to avoid cross-test contamination.
    await repo.upsert(SnapshotUpsert(
        market="us", symbol="T_COV_FRESH", snapshot_date=today,
        latest_close=Decimal("78500"), closes_window=[78500], source="yahoo",
    ))
    await repo.upsert(SnapshotUpsert(
        market="us", symbol="T_COV_STALE", snapshot_date=dt.date(2026, 5, 1),
        latest_close=Decimal("130000"), closes_window=[130000], source="yahoo",
    ))
    await db_session.commit()

    cov = await repo.coverage(market="us", today_trading_date=today)
    assert cov.fresh_count == 1
    assert cov.stale_count == 1

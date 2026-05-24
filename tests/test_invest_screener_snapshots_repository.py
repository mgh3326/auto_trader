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
    # Use a synthetic numeric KR-like symbol: some full-suite fixtures clean up
    # non-market-shaped tickers, and this test commits mid-test to verify upsert.
    symbol = "900101"
    payload = SnapshotUpsert(
        market="kr",
        symbol=symbol,
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

    rows = await repo.get_fresh(
        market="kr", symbols=[symbol], on_or_after=dt.date(2026, 5, 9)
    )
    assert len(rows) == 1
    assert rows[0].consecutive_up_days == 4


@pytest.mark.asyncio
async def test_get_fresh_filters_stale(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    # Use a symbol distinct from other tests to avoid cross-test contamination.
    await repo.upsert(
        SnapshotUpsert(
            market="kr",
            symbol="T_STALE_001",
            snapshot_date=dt.date(2026, 5, 1),
            latest_close=Decimal("70000"),
            closes_window=[70000],
            source="kis",
        )
    )
    await db_session.commit()

    rows = await repo.get_fresh(
        market="kr", symbols=["T_STALE_001"], on_or_after=dt.date(2026, 5, 9)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_coverage_counts(db_session):
    from sqlalchemy import text

    await db_session.execute(
        text(
            "DELETE FROM invest_screener_snapshots WHERE symbol IN ('T_TOP_A', 'T_TOP_B', 'T_TOP_OLD')"
        )
    )
    await db_session.commit()

    repo = InvestScreenerSnapshotsRepository(db_session)
    today = dt.date(2026, 5, 9)
    # Use symbols distinct from other tests to avoid cross-test contamination.
    await repo.upsert(
        SnapshotUpsert(
            market="us",
            symbol="T_COV_FRESH",
            snapshot_date=today,
            latest_close=Decimal("78500"),
            closes_window=[78500],
            source="yahoo",
        )
    )
    await repo.upsert(
        SnapshotUpsert(
            market="us",
            symbol="T_COV_STALE",
            snapshot_date=dt.date(2026, 5, 1),
            latest_close=Decimal("130000"),
            closes_window=[130000],
            source="yahoo",
        )
    )
    await db_session.commit()

    cov = await repo.coverage(market="us", today_trading_date=today)
    assert cov.fresh_count == 1
    assert cov.stale_count == 1


@pytest.mark.asyncio
async def test_list_top_candidates_orders_by_change_rate_from_latest_partition(
    db_session,
):
    from sqlalchemy import text

    # Clean up any persistent dirty rows from previous runs.
    await db_session.execute(
        text(
            "DELETE FROM invest_screener_snapshots WHERE symbol IN ('T_TOP_A', 'T_TOP_B', 'T_TOP_OLD')"
        )
    )
    await db_session.commit()

    repo = InvestScreenerSnapshotsRepository(db_session)
    base = {"market": "kr", "snapshot_date": dt.date(2030, 5, 22), "source": "yahoo"}
    await repo.upsert(
        SnapshotUpsert(
            symbol="T_TOP_A",
            latest_close=Decimal("10"),
            change_rate=Decimal("1.0"),
            closes_window=[10],
            **base,
        )
    )
    await repo.upsert(
        SnapshotUpsert(
            symbol="T_TOP_B",
            latest_close=Decimal("10"),
            change_rate=Decimal("9.0"),
            closes_window=[10],
            **base,
        )
    )
    # An older partition row that must be excluded (not latest).
    await repo.upsert(
        SnapshotUpsert(
            symbol="T_TOP_OLD",
            latest_close=Decimal("10"),
            change_rate=Decimal("50.0"),
            closes_window=[10],
            market="kr",
            snapshot_date=dt.date(2030, 5, 1),
            source="yahoo",
        )
    )
    await db_session.commit()

    rows = await repo.list_top_candidates(market="kr", limit=10)
    syms = [r.symbol for r in rows if r.symbol in {"T_TOP_A", "T_TOP_B", "T_TOP_OLD"}]
    assert syms == ["T_TOP_B", "T_TOP_A"]  # latest partition only, change_rate desc


@pytest.mark.asyncio
async def test_breadth_counts_advancers_decliners_in_latest_partition(db_session):
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = dict(market="us", snapshot_date=dt.date(2026, 5, 23), source="yahoo")
    await repo.upsert(SnapshotUpsert(symbol="T_BR_UP1", latest_close=Decimal("10"),
                                     change_rate=Decimal("2.0"), closes_window=[10], **base))
    await repo.upsert(SnapshotUpsert(symbol="T_BR_UP2", latest_close=Decimal("10"),
                                     change_rate=Decimal("0.5"), closes_window=[10], **base))
    await repo.upsert(SnapshotUpsert(symbol="T_BR_DN1", latest_close=Decimal("10"),
                                     change_rate=Decimal("-1.0"), closes_window=[10], **base))
    await db_session.commit()

    b = await repo.breadth(market="us")
    assert b.advancers >= 2
    assert b.decliners >= 1
    assert b.total == b.advancers + b.decliners + b.unchanged

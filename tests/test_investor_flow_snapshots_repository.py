from __future__ import annotations

import datetime as dt

import pytest

from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
    InvestorFlowSnapshotUpsert,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_and_latest_by_symbols_returns_fresh_snapshot(db_session):
    repo = InvestorFlowSnapshotsRepository(db_session)
    snapshot_date = dt.date(2026, 5, 11)

    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol="900191",
            snapshot_date=snapshot_date,
            foreign_net=1_200_000,
            institution_net=300_000,
            individual_net=-1_500_000,
            foreign_holding_shares=50_000_000,
            foreign_holding_rate=15.25,
            foreign_net_buy_rank=7,
            institution_net_buy_rank=12,
            foreign_consecutive_buy_days=3,
            institution_consecutive_buy_days=1,
            source="naver_finance",
            collected_at=dt.datetime(2026, 5, 11, 6, 30, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    rows = await repo.latest_by_symbols(
        market="kr", symbols=["900191"], as_of=snapshot_date
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "900191"
    assert row.foreign_net == 1_200_000
    assert row.foreign_holding_shares == 50_000_000
    assert float(row.foreign_holding_rate) == 15.25
    assert row.institution_net == 300_000
    assert row.individual_net == -1_500_000
    assert row.foreign_net_buy_rank == 7
    assert row.institution_net_buy_rank == 12
    assert row.double_buy is True
    assert row.double_sell is False
    assert row.foreign_consecutive_buy_days == 3
    assert row.source == "naver_finance"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_by_symbols_picks_newest_snapshot_per_symbol(db_session):
    repo = InvestorFlowSnapshotsRepository(db_session)
    symbol = "900192"
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol=symbol,
            snapshot_date=dt.date(2026, 5, 9),
            foreign_net=-100,
            institution_net=-200,
            individual_net=300,
            source="naver_finance",
        )
    )
    await repo.upsert(
        InvestorFlowSnapshotUpsert(
            market="kr",
            symbol=symbol,
            snapshot_date=dt.date(2026, 5, 11),
            foreign_net=100,
            institution_net=200,
            individual_net=-300,
            source="naver_finance",
        )
    )
    await db_session.commit()

    rows = await repo.latest_by_symbols(
        market="kr", symbols=[symbol], as_of=dt.date(2026, 5, 11)
    )

    assert len(rows) == 1
    assert rows[0].snapshot_date == dt.date(2026, 5, 11)
    assert rows[0].double_buy is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recent_by_symbol_returns_descending_daily_history(db_session):
    repo = InvestorFlowSnapshotsRepository(db_session)
    symbol = "900193"
    for day, foreign_net in [(9, 90), (10, -10), (11, 110)]:
        await repo.upsert(
            InvestorFlowSnapshotUpsert(
                market="kr",
                symbol=symbol,
                snapshot_date=dt.date(2026, 5, day),
                foreign_net=foreign_net,
                institution_net=day,
                individual_net=-foreign_net,
                source="naver_finance",
            )
        )
    await db_session.commit()

    rows = await repo.recent_by_symbol(
        market="kr", symbol=symbol, as_of=dt.date(2026, 5, 11), limit=2
    )

    assert [row.snapshot_date for row in rows] == [
        dt.date(2026, 5, 11),
        dt.date(2026, 5, 10),
    ]
    assert rows[0].foreign_net == 110

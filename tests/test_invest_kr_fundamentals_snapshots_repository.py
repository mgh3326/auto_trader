from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
    KrFundamentalsSnapshotUpsert,
)

# Synthetic KR-shaped codes distinct from any production-shaped symbols so the
# full-suite cleanup fixtures don't collide with these rows.
_SYM_A = "990001"
_SYM_B = "990002"
_SYM_OLD = "990003"


async def _cleanup(db_session) -> None:
    await db_session.execute(
        text(
            "DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol IN (:a, :b, :c)"
        ),
        {"a": _SYM_A, "b": _SYM_B, "c": _SYM_OLD},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_idempotently(db_session):
    await _cleanup(db_session)
    repo = InvestKrFundamentalsSnapshotsRepository(db_session)
    snapshot_date = dt.date(2026, 6, 4)

    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_A,
            snapshot_date=snapshot_date,
            name="테스트종목",
            price=Decimal("10000"),
            roe_ttm=Decimal("12.5"),
            per=Decimal("8.1"),
            dividend_yield=Decimal("3.2"),
            sector="Finance",
            industry="Banks",
        )
    )
    await db_session.commit()

    first = (
        await db_session.execute(
            text(
                "SELECT computed_at FROM invest_kr_fundamentals_snapshots "
                "WHERE symbol = :s AND snapshot_date = :d"
            ),
            {"s": _SYM_A, "d": snapshot_date},
        )
    ).scalar_one()

    # Re-upsert with a changed metric on the same (symbol, snapshot_date).
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_A,
            snapshot_date=snapshot_date,
            name="테스트종목",
            price=Decimal("11000"),
            roe_ttm=Decimal("13.5"),
        )
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            text(
                "SELECT price, roe_ttm, computed_at "
                "FROM invest_kr_fundamentals_snapshots "
                "WHERE symbol = :s AND snapshot_date = :d"
            ),
            {"s": _SYM_A, "d": snapshot_date},
        )
    ).all()
    assert len(rows) == 1, "upsert must be idempotent on (symbol, snapshot_date)"
    assert rows[0].price == Decimal("11000")
    assert rows[0].roe_ttm == Decimal("13.5")
    assert rows[0].computed_at >= first


@pytest.mark.asyncio
async def test_latest_partition_returns_max_date(db_session):
    await _cleanup(db_session)
    repo = InvestKrFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_OLD,
            snapshot_date=dt.date(2026, 6, 1),
            price=Decimal("100"),
        )
    )
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_A,
            snapshot_date=dt.date(2026, 6, 4),
            price=Decimal("100"),
        )
    )
    await db_session.commit()

    latest = await repo.latest_partition()
    assert latest is not None
    assert latest >= dt.date(2026, 6, 4)


@pytest.mark.asyncio
async def test_coverage_counts_latest_and_stale(db_session):
    await _cleanup(db_session)
    repo = InvestKrFundamentalsSnapshotsRepository(db_session)
    # coverage() is table-wide. Keep this partition ahead of other committed
    # fixture rows that may exist in a parallel xdist worker, but leave these
    # rows uncommitted so this test does not leak its own latest partition.
    today = dt.date(2099, 1, 1)
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_A,
            snapshot_date=today,
            price=Decimal("100"),
        )
    )
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_B,
            snapshot_date=today,
            price=Decimal("100"),
        )
    )
    await repo.upsert(
        KrFundamentalsSnapshotUpsert(
            symbol=_SYM_OLD,
            snapshot_date=dt.date(2098, 12, 31),
            price=Decimal("100"),
        )
    )

    coverage = await repo.coverage(today=today)
    assert coverage.latest_partition_date is not None
    assert coverage.latest_partition_date >= today
    # The two today rows are in the latest partition.
    assert coverage.latest_partition_count >= 2
    # The 2098-12-31 row is strictly before today → stale.
    assert coverage.stale_count >= 1
    assert coverage.last_computed_at is not None

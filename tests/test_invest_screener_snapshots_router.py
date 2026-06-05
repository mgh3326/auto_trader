"""Coverage endpoint tests for invest_screener_snapshots (ROB-170 Task 7)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)


@pytest.mark.asyncio
async def test_coverage_report_structure(db_session):
    """CoverageReport always returns the required fields and a valid DataState."""
    from app.services.invest_screener_snapshots.coverage_service import build_coverage

    report = await build_coverage(db_session, market="us")
    assert report.market == "us"
    assert isinstance(report.snapshotsCoveringToday, int)
    assert isinstance(report.snapshotsStale, int)
    assert isinstance(report.snapshotsMissing, int)
    assert isinstance(report.totalSymbolsInUniverse, int)
    assert report.dataState in {"fresh", "partial", "stale", "missing", "fallback"}
    # When there are no fresh snapshots, state must NOT be "fresh"
    if report.snapshotsCoveringToday == 0:
        assert report.dataState != "fresh"


@pytest.mark.asyncio
async def test_coverage_counts_fresh_and_stale(db_session):
    from app.services.invest_screener_snapshots.coverage_service import build_coverage
    from app.services.invest_screener_snapshots.freshness import (
        expected_baseline_date,
    )

    repo = InvestScreenerSnapshotsRepository(db_session)
    # ROB-438 follow-up: build_coverage classifies against the session-aware
    # baseline; seed the "fresh" row on the same baseline so the test holds in the
    # KR pre-market window too (where baseline = prior trading day != calendar today).
    today = expected_baseline_date("kr")
    await repo.upsert(
        SnapshotUpsert(
            market="kr",
            symbol="COV_FRESH_001",
            snapshot_date=today,
            latest_close=Decimal("78500"),
            closes_window=[77000, 77400, 77900, 78500, 78500],
            source="kis",
        )
    )
    await repo.upsert(
        SnapshotUpsert(
            market="kr",
            symbol="COV_STALE_001",
            snapshot_date=dt.date(2026, 1, 1),
            latest_close=Decimal("130000"),
            closes_window=[130000],
            source="kis",
        )
    )
    await db_session.commit()

    report = await build_coverage(db_session, market="kr")
    assert report.snapshotsCoveringToday >= 1
    assert report.snapshotsStale >= 1

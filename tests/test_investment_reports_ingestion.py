"""ROB-265 Plan 2 — Ingestion service tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.base import Base
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    WatchConditionPayload,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
    InvestmentReportItemDecision.__table__,
    InvestmentWatchAlert.__table__,
    InvestmentWatchEvent.__table__,
]


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=_ALL_TABLES, checkfirst=True
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as sess:
            try:
                yield sess
            finally:
                await sess.rollback()
        async with factory() as cleanup:
            for table in reversed(_ALL_TABLES):
                await cleanup.execute(
                    sa.text(
                        f'TRUNCATE TABLE review."{table.name}" RESTART IDENTITY CASCADE'
                    )
                )
            await cleanup.commit()
    finally:
        await engine.dispose()


def _future(days: int = 7) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _base_request(**overrides) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "테스트",
        "summary": "요약",
        "kst_date": "2026-05-18",
        "generator_version": "v1",
    }
    payload.update(overrides)
    return IngestReportRequest(**payload)


@pytest.mark.asyncio
async def test_ingest_creates_report_with_items(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(
        items=[
            IngestReportItem(
                item_kind="action",
                symbol="005930",
                side="buy",
                intent="buy_review",
                rationale="r",
            ),
            IngestReportItem(
                item_kind="watch",
                symbol="000660",
                intent="trend_recovery_review",
                rationale="r",
                watch_condition=WatchConditionPayload(
                    metric="rsi", operator="below", threshold=30
                ),
                valid_until=_future(),
            ),
        ]
    )
    report = await service.ingest(request)
    assert report.id is not None
    assert report.report_uuid is not None

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    kinds = {it.item_kind for it in items}
    assert kinds == {"action", "watch"}


@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_same_key(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(
        items=[
            IngestReportItem(
                item_kind="action",
                symbol="005930",
                side="buy",
                intent="buy_review",
                rationale="r",
            )
        ]
    )

    first = await service.ingest(request)
    second = await service.ingest(request)

    assert first.report_uuid == second.report_uuid
    assert first.id == second.id

    # No duplicate items.
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(first.id)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_different_kst_date_creates_distinct_report(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    a = await service.ingest(_base_request(kst_date="2026-05-18"))
    b = await service.ingest(_base_request(kst_date="2026-05-19"))
    assert a.id != b.id
    assert a.report_uuid != b.report_uuid


@pytest.mark.asyncio
async def test_watch_condition_stored_as_jsonb(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(
        items=[
            IngestReportItem(
                item_kind="watch",
                symbol="005930",
                intent="trend_recovery_review",
                rationale="r",
                watch_condition=WatchConditionPayload(
                    metric="rsi", operator="below", threshold=30
                ),
                valid_until=_future(),
            )
        ]
    )
    report = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert items[0].watch_condition is not None
    assert items[0].watch_condition["metric"] == "rsi"
    # threshold_key defaulted from threshold via WatchConditionPayload validator
    assert items[0].watch_condition["threshold_key"] == "30"


@pytest.mark.asyncio
async def test_ingest_advisory_only_invariant_at_schema(
    session: AsyncSession,
) -> None:
    """kis_live + mock_preview is rejected before hitting the DB."""
    with pytest.raises(Exception) as exc_info:
        _base_request(account_scope="kis_live", execution_mode="mock_preview")
    assert "advisory_only" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ingest_kis_live_advisory_only_succeeds(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(
        _base_request(account_scope="kis_live", execution_mode="advisory_only")
    )
    assert report.account_scope == "kis_live"
    assert report.execution_mode == "advisory_only"


@pytest.mark.asyncio
async def test_item_idempotency_key_is_deterministic(
    session: AsyncSession,
) -> None:
    """Re-ingest with the same payload doesn't duplicate items."""
    service = InvestmentReportIngestionService(session)
    request = _base_request(
        items=[
            IngestReportItem(
                item_kind="watch",
                symbol="005930",
                intent="trend_recovery_review",
                rationale="r",
                watch_condition=WatchConditionPayload(
                    metric="rsi", operator="below", threshold=30
                ),
                valid_until=_future(),
            )
        ]
    )
    first = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    first_items = await repo.list_items_for_report(first.id)
    assert len(first_items) == 1
    first_item_idempotency = first_items[0].idempotency_key

    # Re-ingest returns the same report; same idempotency key on items.
    second = await service.ingest(request)
    assert second.id == first.id
    second_items = await repo.list_items_for_report(second.id)
    assert len(second_items) == 1
    assert second_items[0].idempotency_key == first_item_idempotency

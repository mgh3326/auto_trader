"""ROB-265 Plan 2 — Watch activation service tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    ActivateWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    WatchConditionPayload,
)
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService

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


async def _seed_approved_watch_item(
    session: AsyncSession,
) -> InvestmentReportItem:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            market_session="regular",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    item_kind="watch",
                    symbol="005930",
                    intent="trend_recovery_review",
                    rationale="r",
                    watch_condition=WatchConditionPayload(
                        metric="rsi", operator="below", threshold=Decimal("30")
                    ),
                    valid_until=_future(),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = items[0]

    # Approve it so activation is allowed.
    decisions = InvestmentReportDecisionService(session)
    await decisions.record(
        RecordDecisionRequest(
            item_uuid=watch_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    refreshed = await repo.get_item_by_uuid(watch_item.item_uuid)
    assert refreshed is not None
    assert refreshed.status == "approved"
    return refreshed


@pytest.mark.asyncio
async def test_activate_copies_snapshot_and_transitions_item(
    session: AsyncSession,
) -> None:
    item = await _seed_approved_watch_item(session)
    service = WatchActivationService(session)
    alert = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    assert alert.market == "kr"
    assert alert.target_kind == "asset"
    assert alert.symbol == "005930"
    assert alert.metric == "rsi"
    assert alert.operator == "below"
    assert Decimal(alert.threshold) == Decimal("30")
    assert alert.threshold_key == "30"
    assert alert.intent == "trend_recovery_review"
    assert alert.action_mode == "notify_only"
    assert alert.status == "active"
    assert alert.source_item_uuid == item.item_uuid

    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "activated"


@pytest.mark.asyncio
async def test_activate_is_idempotent_per_source_item(
    session: AsyncSession,
) -> None:
    item = await _seed_approved_watch_item(session)
    service = WatchActivationService(session)
    first = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    second = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    assert first.alert_uuid == second.alert_uuid
    assert first.id == second.id


@pytest.mark.asyncio
async def test_activate_rejects_non_watch_item(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="t",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    item_kind="action",
                    symbol="005930",
                    side="buy",
                    intent="buy_review",
                    rationale="r",
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    action_item = items[0]
    # Approve so the only remaining failure is item_kind != watch.
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=action_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    service = WatchActivationService(session)
    with pytest.raises(ValueError, match="only watch items"):
        await service.activate(
            ActivateWatchRequest(item_uuid=action_item.item_uuid, actor="operator-test")
        )


@pytest.mark.asyncio
async def test_activate_rejects_unapproved_watch(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="t",
            title="t",
            summary="s",
            kst_date="2026-05-18",
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
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = items[0]
    # Skip approval — status stays 'proposed'.
    service = WatchActivationService(session)
    with pytest.raises(ValueError, match="only approved items"):
        await service.activate(
            ActivateWatchRequest(item_uuid=watch_item.item_uuid, actor="operator-test")
        )


@pytest.mark.asyncio
async def test_activate_rejects_unknown_item(session: AsyncSession) -> None:
    service = WatchActivationService(session)
    with pytest.raises(ValueError, match="item not found"):
        await service.activate(
            ActivateWatchRequest(item_uuid=uuid.uuid4(), actor="operator-test")
        )


@pytest.mark.asyncio
async def test_alert_snapshot_does_not_mutate_when_item_changes(
    session: AsyncSession,
) -> None:
    """Once activated, mutating the source item does not change the alert."""
    item = await _seed_approved_watch_item(session)
    service = WatchActivationService(session)
    alert = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    original_rationale = alert.rationale
    alert_key = alert.idempotency_key

    repo = InvestmentReportsRepository(session)
    await session.execute(
        sa.update(InvestmentReportItem)
        .where(InvestmentReportItem.id == item.id)
        .values(rationale="something else entirely")
    )
    await session.commit()

    refreshed_alert = await repo.get_alert_by_idempotency_key(alert_key)
    assert refreshed_alert is not None
    assert refreshed_alert.rationale == original_rationale

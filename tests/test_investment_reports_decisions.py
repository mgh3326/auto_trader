"""ROB-265 Plan 2 — Decisions service tests."""

from __future__ import annotations

import uuid

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
    RecordDecisionRequest,
)
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
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


async def _seed_report_with_one_action_item(
    session: AsyncSession, *, kst_date: str = "2026-05-18"
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
            kst_date=kst_date,
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
    return items[0]


@pytest.mark.asyncio
async def test_approve_records_decision_and_transitions_item(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)

    decision = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    assert decision.decision == "approve"
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed is not None
    assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_deny_transitions_item_to_denied(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="deny", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "denied"


@pytest.mark.asyncio
async def test_defer_transitions_item_to_deferred(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="defer", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "deferred"


@pytest.mark.asyncio
async def test_skip_leaves_item_status_unchanged(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="skip", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    # skip is audit-only — item stays proposed.
    assert refreshed.status == "proposed"


@pytest.mark.asyncio
async def test_partial_approve_transitions_to_approved_with_snapshot(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid,
            decision="partial_approve",
            actor="operator-test",
            approved_payload_snapshot={"max_notional_krw": 100000},
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "approved"
    decisions = await repo.list_decisions_for_item(item.id)
    assert decisions[0].approved_payload_snapshot == {"max_notional_krw": 100000}


@pytest.mark.asyncio
async def test_record_is_idempotent_per_default_key(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    first = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    second = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    assert first.id == second.id
    repo = InvestmentReportsRepository(session)
    decisions = await repo.list_decisions_for_item(item.id)
    assert len(decisions) == 1


@pytest.mark.asyncio
async def test_multiple_decisions_per_item_persist(session: AsyncSession) -> None:
    """defer → approve produces two rows; final status reflects latest verb."""
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="defer", actor="operator-test"
        )
    )
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    decisions = await repo.list_decisions_for_item(item.id)
    assert {d.decision for d in decisions} == {"defer", "approve"}
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_unknown_item_uuid_raises(session: AsyncSession) -> None:
    service = InvestmentReportDecisionService(session)
    with pytest.raises(ValueError):
        await service.record(
            RecordDecisionRequest(
                item_uuid=uuid.uuid4(),  # not seeded
                decision="approve",
                actor="operator-test",
            )
        )

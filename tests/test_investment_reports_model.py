"""ROB-265 — InvestmentReport ORM + advisory-only invariant tests.

These exercise DB-level CHECK constraints, so they require the real
PostgreSQL configured by ``tests/conftest.py``. The fixture creates and
drops the new ``investment_*`` tables per test against the live test DB,
isolated from any migration-managed tables.
"""

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
)

_ALL_TABLES = [
    InvestmentReport.__table__,
    InvestmentReportItem.__table__,
]


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Per-test session with table-managed clean state.

    Tables are created once (idempotent if migration already owns them) and
    truncated between tests. Avoids ``Base.metadata.drop_all`` which would
    try to drop the shared ``instrument_type`` enum used by other models.
    """
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


def _base_payload(**overrides) -> dict:
    payload = dict(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"key-{uuid.uuid4()}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="test",
        title="테스트 리포트",
        summary="요약",
        status="draft",
    )
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_round_trip_insert(session: AsyncSession) -> None:
    row = InvestmentReport(**_base_payload())
    session.add(row)
    await session.commit()

    result = await session.execute(
        sa.select(InvestmentReport).where(InvestmentReport.id == row.id)
    )
    fetched = result.scalar_one()
    assert fetched.market == "kr"
    assert fetched.execution_mode == "mock_preview"
    assert fetched.market_snapshot == {}
    assert fetched.report_metadata == {}


@pytest.mark.asyncio
async def test_idempotency_key_is_unique(session: AsyncSession) -> None:
    key = f"dup-{uuid.uuid4()}"
    session.add(InvestmentReport(**_base_payload(idempotency_key=key)))
    await session.commit()

    session.add(InvestmentReport(**_base_payload(idempotency_key=key)))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_advisory_only_invariant_blocks_live_with_mock_preview(
    session: AsyncSession,
) -> None:
    """kis_live account scope MUST pair with execution_mode='advisory_only'."""
    session.add(
        InvestmentReport(
            **_base_payload(
                account_scope="kis_live",
                execution_mode="mock_preview",
            )
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_advisory_only_invariant_allows_live_with_advisory_only(
    session: AsyncSession,
) -> None:
    row = InvestmentReport(
        **_base_payload(
            account_scope="kis_live",
            execution_mode="advisory_only",
        )
    )
    session.add(row)
    await session.commit()
    assert row.id is not None


@pytest.mark.asyncio
async def test_nxt_session_requires_advisory_only(session: AsyncSession) -> None:
    session.add(
        InvestmentReport(
            **_base_payload(
                market_session="nxt",
                execution_mode="mock_preview",
            )
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_status_check_rejects_unknown_value(session: AsyncSession) -> None:
    session.add(InvestmentReport(**_base_payload(status="bogus")))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


# ---------------------------------------------------------------------------
# InvestmentReportItem
# ---------------------------------------------------------------------------
async def _make_report(session: AsyncSession, **overrides) -> InvestmentReport:
    row = InvestmentReport(**_base_payload(**overrides))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _base_item_payload(report_id: int, **overrides) -> dict:
    payload = dict(
        report_id=report_id,
        item_uuid=uuid.uuid4(),
        idempotency_key=f"item-{uuid.uuid4()}",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        target_kind="asset",
        priority=10,
        rationale="정규장 확인 후 수동 승인 후보",
    )
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_item_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert item.status == "proposed"
    assert item.target_kind == "asset"
    assert item.trigger_checklist == []


@pytest.mark.asyncio
async def test_item_kind_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(
        InvestmentReportItem(**_base_item_payload(report.id, item_kind="bogus"))
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_watch_item_requires_condition(session: AsyncSession) -> None:
    report = await _make_report(session)
    # Missing watch_condition for item_kind='watch' → violation.
    session.add(
        InvestmentReportItem(
            **_base_item_payload(report.id, item_kind="watch", side=None)
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_watch_item_with_condition_inserts(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(
        **_base_item_payload(
            report.id,
            item_kind="watch",
            side=None,
            intent="trend_recovery_review",
            watch_condition={
                "metric": "rsi",
                "operator": "below",
                "threshold": 30,
                "target_kind": "asset",
            },
        )
    )
    session.add(item)
    await session.commit()
    assert item.watch_condition["metric"] == "rsi"


@pytest.mark.asyncio
async def test_target_kind_check_rejects_unknown(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(
        InvestmentReportItem(
            **_base_item_payload(report.id, target_kind="commodity")
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_cascade_delete_from_report(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    await session.commit()

    await session.delete(report)
    await session.commit()

    remaining = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItem)
    )
    assert remaining == 0

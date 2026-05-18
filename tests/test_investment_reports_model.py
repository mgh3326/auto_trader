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
from app.models.investment_reports import InvestmentReport

_ALL_TABLES = [
    InvestmentReport.__table__,
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

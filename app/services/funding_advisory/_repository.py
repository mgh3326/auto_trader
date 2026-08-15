"""Private persistence adapter for funding advisory threads and revisions."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.funding_advisory import (
    FundingAdvisory,
    FundingAdvisoryDelivery,
    FundingAdvisoryRevision,
)


class FundingAdvisoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_lock(self, lock_key: str) -> None:
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )

    async def get_advisory_by_thread(
        self, thread_key: str, *, for_update: bool = False
    ) -> FundingAdvisory | None:
        stmt = select(FundingAdvisory).where(FundingAdvisory.thread_key == thread_key)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_advisory(
        self,
        advisory_id: UUID,
        *,
        owner_user_id: int | None = None,
        for_update: bool = False,
    ) -> FundingAdvisory | None:
        stmt = select(FundingAdvisory).where(FundingAdvisory.advisory_id == advisory_id)
        if owner_user_id is not None:
            stmt = stmt.where(FundingAdvisory.owner_user_id == owner_user_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def insert_advisory(self, **columns: Any) -> FundingAdvisory:
        row = FundingAdvisory(**columns)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_advisory(self, row: FundingAdvisory, **columns: Any) -> None:
        for key, value in columns.items():
            setattr(row, key, value)
        await self._session.flush()

    async def latest_revision(
        self, advisory_id: UUID
    ) -> FundingAdvisoryRevision | None:
        stmt = (
            select(FundingAdvisoryRevision)
            .where(FundingAdvisoryRevision.advisory_id == advisory_id)
            .order_by(FundingAdvisoryRevision.revision_no.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_revision_by_fingerprint(
        self, *, advisory_id: UUID, fingerprint: str
    ) -> FundingAdvisoryRevision | None:
        stmt = select(FundingAdvisoryRevision).where(
            FundingAdvisoryRevision.advisory_id == advisory_id,
            FundingAdvisoryRevision.fingerprint == fingerprint,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def insert_revision(self, **columns: Any) -> FundingAdvisoryRevision:
        row = FundingAdvisoryRevision(**columns)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def list_advisories(
        self, *, owner_user_id: int, state: str | None = None, limit: int = 100
    ) -> list[FundingAdvisory]:
        stmt = (
            select(FundingAdvisory)
            .where(FundingAdvisory.owner_user_id == owner_user_id)
            .order_by(FundingAdvisory.updated_at.desc())
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(FundingAdvisory.state == state)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_delivery(
        self,
        *,
        advisory_id: UUID,
        channel: str,
        kst_date: date,
        for_update: bool = False,
    ) -> FundingAdvisoryDelivery | None:
        stmt = select(FundingAdvisoryDelivery).where(
            FundingAdvisoryDelivery.advisory_id == advisory_id,
            FundingAdvisoryDelivery.channel == channel,
            FundingAdvisoryDelivery.kst_date == kst_date,
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_delivery_by_id(
        self, delivery_id: UUID, *, for_update: bool = False
    ) -> FundingAdvisoryDelivery | None:
        stmt = select(FundingAdvisoryDelivery).where(
            FundingAdvisoryDelivery.delivery_id == delivery_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def insert_delivery(self, **columns: Any) -> FundingAdvisoryDelivery:
        row = FundingAdvisoryDelivery(**columns)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_delivery(
        self, row: FundingAdvisoryDelivery, **columns: Any
    ) -> None:
        for key, value in columns.items():
            setattr(row, key, value)
        await self._session.flush()

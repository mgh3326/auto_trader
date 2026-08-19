"""Private persistence adapter for external-cash declarations.

Never commits.  The public service owns transaction completion, and no caller
outside ``external_cash.py`` may use this repository to write the ledger.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.funding_advisory import ExternalCashDeclaration


class ExternalCashDeclarationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire_lock(self, lock_key: str) -> None:
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )

    async def get_by_idempotency(
        self, *, owner_user_id: int, idempotency_key: str
    ) -> ExternalCashDeclaration | None:
        stmt = select(ExternalCashDeclaration).where(
            ExternalCashDeclaration.owner_user_id == owner_user_id,
            ExternalCashDeclaration.idempotency_key == idempotency_key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_current_heads(
        self,
        *,
        owner_user_id: int,
        location_key: str,
        currency: str,
        for_update: bool = False,
    ) -> list[ExternalCashDeclaration]:
        child = aliased(ExternalCashDeclaration)
        stmt = (
            select(ExternalCashDeclaration)
            .outerjoin(
                child,
                child.supersedes_declaration_id
                == ExternalCashDeclaration.declaration_id,
            )
            .where(
                ExternalCashDeclaration.owner_user_id == owner_user_id,
                ExternalCashDeclaration.location_key == location_key,
                ExternalCashDeclaration.currency == currency,
                child.id.is_(None),
            )
            .order_by(
                ExternalCashDeclaration.as_of.desc(),
                ExternalCashDeclaration.recorded_at.desc(),
            )
        )
        if for_update:
            stmt = stmt.with_for_update(of=ExternalCashDeclaration)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_all_current_heads(
        self, *, owner_user_id: int
    ) -> list[ExternalCashDeclaration]:
        child = aliased(ExternalCashDeclaration)
        stmt = (
            select(ExternalCashDeclaration)
            .outerjoin(
                child,
                child.supersedes_declaration_id
                == ExternalCashDeclaration.declaration_id,
            )
            .where(
                ExternalCashDeclaration.owner_user_id == owner_user_id,
                child.id.is_(None),
            )
            .order_by(
                ExternalCashDeclaration.location_key,
                ExternalCashDeclaration.currency,
                ExternalCashDeclaration.as_of.desc(),
                ExternalCashDeclaration.recorded_at.desc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_history(
        self,
        *,
        owner_user_id: int,
        location_key: str,
        currency: str,
        limit: int,
    ) -> list[ExternalCashDeclaration]:
        stmt = (
            select(ExternalCashDeclaration)
            .where(
                ExternalCashDeclaration.owner_user_id == owner_user_id,
                ExternalCashDeclaration.location_key == location_key,
                ExternalCashDeclaration.currency == currency,
            )
            .order_by(
                ExternalCashDeclaration.as_of.desc(),
                ExternalCashDeclaration.recorded_at.desc(),
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def insert(self, **columns: Any) -> ExternalCashDeclaration:
        row = ExternalCashDeclaration(**columns)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

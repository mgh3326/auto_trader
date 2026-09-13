"""Repository for the Phase 0 UUID outcome table only."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fill_watch_context_outcome import FillWatchContextOutcome

__all__ = ["FillWatchContextOutcomeRepository"]


@dataclass
class FillWatchContextOutcomeRepository:
    """The only persistence adapter used by the context outcome service."""

    session: AsyncSession

    async def get_by_transport_uuid(
        self,
        transport_event_uuid: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FillWatchContextOutcome | None:
        statement = sa.select(FillWatchContextOutcome).where(
            FillWatchContextOutcome.transport_event_uuid == transport_event_uuid
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def list_by_economic_root(
        self, economic_root_ref: str
    ) -> list[FillWatchContextOutcome]:
        rows = await self.session.scalars(
            sa.select(FillWatchContextOutcome)
            .where(FillWatchContextOutcome.economic_root_ref == economic_root_ref)
            .order_by(
                FillWatchContextOutcome.input_as_of.asc(),
                FillWatchContextOutcome.transport_event_uuid.asc(),
            )
        )
        return list(rows)

    def add(self, row: FillWatchContextOutcome) -> None:
        """Stage a row; transaction ownership remains with the service."""
        self.session.add(row)

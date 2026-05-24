"""Repository for investment_dimension_reports (ROB-306). Service-internal."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_dimension_reports import InvestmentDimensionReport


class DimensionReportPersistRace(RuntimeError):
    pass


class DimensionReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, key: str
    ) -> InvestmentDimensionReport | None:
        result = await self._session.execute(
            select(InvestmentDimensionReport).where(
                InvestmentDimensionReport.idempotency_key == key
            )
        )
        return result.scalar_one_or_none()

    async def next_version(
        self, *, run_uuid: uuid.UUID, dimension: str, market: str, symbol: str | None
    ) -> int:
        stmt = select(func.max(InvestmentDimensionReport.artifact_version)).where(
            InvestmentDimensionReport.run_uuid == run_uuid,
            InvestmentDimensionReport.dimension == dimension,
            InvestmentDimensionReport.market == market,
        )
        stmt = stmt.where(
            InvestmentDimensionReport.symbol.is_(None)
            if symbol is None
            else InvestmentDimensionReport.symbol == symbol
        )
        result = await self._session.execute(stmt)
        return int((result.scalar_one_or_none() or 0) + 1)

    async def persist(self, **fields: Any) -> InvestmentDimensionReport:
        row = InvestmentDimensionReport(**fields)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_run(
        self, run_uuid: uuid.UUID
    ) -> list[InvestmentDimensionReport]:
        result = await self._session.execute(
            select(InvestmentDimensionReport)
            .where(InvestmentDimensionReport.run_uuid == run_uuid)
            .order_by(
                InvestmentDimensionReport.dimension,
                InvestmentDimensionReport.artifact_version.desc(),
            )
        )
        return list(result.scalars().all())

    async def get_by_uuids(
        self, dimension_report_uuids: list[uuid.UUID]
    ) -> list[InvestmentDimensionReport]:
        if not dimension_report_uuids:
            return []
        result = await self._session.execute(
            select(InvestmentDimensionReport).where(
                InvestmentDimensionReport.dimension_report_uuid.in_(
                    dimension_report_uuids
                )
            )
        )
        return list(result.scalars().all())

"""Append-only repository for symbol intermediate reports (ROB-301).

Internal to the investment_stages service package. All writes go through
:class:`SymbolIntermediateReportIngestService` (ROB-301 D6 — service-only
writes). Not imported elsewhere.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_symbol_intermediate_reports import (
    InvestmentSymbolIntermediateReport,
)


class SymbolReportPersistRace(Exception):
    """Raised when a concurrent insert collides on a unique key."""


class SymbolIntermediateReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, key: str
    ) -> InvestmentSymbolIntermediateReport | None:
        return await self._session.scalar(
            select(InvestmentSymbolIntermediateReport).where(
                InvestmentSymbolIntermediateReport.idempotency_key == key
            )
        )

    async def next_version(
        self, *, run_uuid: uuid.UUID, symbol: str, report_kind: str
    ) -> int:
        current = await self._session.scalar(
            select(func.max(InvestmentSymbolIntermediateReport.artifact_version)).where(
                InvestmentSymbolIntermediateReport.run_uuid == run_uuid,
                InvestmentSymbolIntermediateReport.symbol == symbol,
                InvestmentSymbolIntermediateReport.report_kind == report_kind,
            )
        )
        return int(current or 0) + 1

    async def list_for_run(
        self, run_uuid: uuid.UUID
    ) -> list[InvestmentSymbolIntermediateReport]:
        result = await self._session.scalars(
            select(InvestmentSymbolIntermediateReport)
            .where(InvestmentSymbolIntermediateReport.run_uuid == run_uuid)
            .order_by(
                InvestmentSymbolIntermediateReport.symbol,
                InvestmentSymbolIntermediateReport.artifact_version,
            )
        )
        return list(result.all())

    async def persist(self, **fields: Any) -> InvestmentSymbolIntermediateReport:
        row = InvestmentSymbolIntermediateReport(**fields)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise SymbolReportPersistRace(str(exc)) from exc
        await self._session.refresh(row)
        return row

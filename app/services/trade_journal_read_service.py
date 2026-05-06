# app/services/trade_journal_read_service.py
"""ROB-121 — Read-only service for trade journal retrospective."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import TradeJournal
from app.schemas.trade_journal import JournalReadResponse
from app.services.trade_journal_write_service import _to_read

_TERMINAL_STATUSES = ("closed", "stopped", "expired")


class TradeJournalReadService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_retrospective(self) -> list[JournalReadResponse]:
        """List all journals in a terminal status for retrospective."""
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.account_type == "live",
                TradeJournal.status.in_(_TERMINAL_STATUSES),
            )
            .order_by(desc(TradeJournal.updated_at))
        )
        result = await self.db.execute(stmt)
        return [_to_read(j) for j in result.scalars().all()]

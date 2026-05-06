# app/services/trade_journal_status_service.py
"""ROB-121 — Service for terminal transitions of trade journals."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_journal import TradeJournal

JournalExitReason = Literal["target", "stop", "manual", "expired"]


class TradeJournalStatusService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def close_active_journal(
        self,
        symbol: str,
        *,
        exit_price: float,
        exit_date: datetime,
        reason: JournalExitReason,
    ) -> int:
        """Find the latest active journal for a symbol and close it.
        
        Returns the number of journals updated (0 or 1).
        """
        stmt = (
            select(TradeJournal)
            .where(
                TradeJournal.symbol == symbol,
                TradeJournal.account_type == "live",
                TradeJournal.status == "active",
            )
            .order_by(desc(TradeJournal.created_at))
            .limit(1)
        )
        journal = (await self.db.execute(stmt)).scalar_one_or_none()
        if not journal:
            return 0

        journal.status = "closed" if reason == "target" else "stopped"
        if reason == "expired":
            journal.status = "expired"

        journal.exit_price = exit_price
        journal.exit_date = exit_date
        journal.exit_reason = reason

        # Calculate PnL if possible
        # This is a bit simplified, usually needs entry price from the journal or trade
        # For now we just set the status and fields.
        
        await self.db.flush()
        return 1

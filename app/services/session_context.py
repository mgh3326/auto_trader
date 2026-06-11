"""Service layer for ROB-516 operator session context entries."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.session_context import OperatorSessionContext
from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral
from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextEntryTypeLiteral,
)


class SessionContextService:
    """Append-only writer and recent-query reader for operator context."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_entries(
        self,
        entries: list[SessionContextAppendEntry],
    ) -> list[OperatorSessionContext]:
        rows: list[OperatorSessionContext] = []
        default_kst_date = now_kst().date()
        for entry in entries:
            row = OperatorSessionContext(
                kst_date=entry.kst_date or default_kst_date,
                market=entry.market,
                account_scope=entry.account_scope,
                entry_type=entry.entry_type,
                title=entry.title,
                body=entry.body,
                refs=entry.refs.model_dump(mode="json", exclude_none=True),
                created_by=entry.created_by,
                session_label=entry.session_label,
            )
            self._session.add(row)
            rows.append(row)
        await self._session.flush()
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def get_recent(
        self,
        *,
        market: MarketLiteral | None = None,
        account_scope: AccountScopeLiteral | None = None,
        kst_date_from: date | None = None,
        entry_type: SessionContextEntryTypeLiteral | None = None,
        limit: int = 20,
    ) -> list[OperatorSessionContext]:
        capped_limit = max(1, min(int(limit), 100))
        stmt = sa.select(OperatorSessionContext).order_by(
            OperatorSessionContext.created_at.desc(),
            OperatorSessionContext.id.desc(),
        )
        if market is not None:
            stmt = stmt.where(OperatorSessionContext.market == market)
        if account_scope is not None:
            stmt = stmt.where(OperatorSessionContext.account_scope == account_scope)
        if kst_date_from is not None:
            stmt = stmt.where(OperatorSessionContext.kst_date >= kst_date_from)
        if entry_type is not None:
            stmt = stmt.where(OperatorSessionContext.entry_type == entry_type)
        result = await self._session.scalars(stmt.limit(capped_limit))
        return list(result.all())

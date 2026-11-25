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
        include_market_wide: bool = False,
    ) -> list[OperatorSessionContext]:
        """Return recent entries, newest first.

        ``include_market_wide`` is an opt-in widening of the ``account_scope``
        filter only. ``account_scope=NULL`` rows mean "market-wide operator
        instruction" and are otherwise invisible to any caller that names a
        concrete scope. Callers that must see them (the run-start operating
        briefing) pass True; the HTTP router and the ``session_context_get_recent``
        MCP tool keep the default False and their strict-equality semantics.
        When ``account_scope`` is None no scope filter is applied at all, so
        this flag has no effect on that call shape.
        """
        capped_limit = max(1, min(int(limit), 100))
        stmt = sa.select(OperatorSessionContext).order_by(
            OperatorSessionContext.created_at.desc(),
            OperatorSessionContext.id.desc(),
        )
        if market is not None:
            stmt = stmt.where(OperatorSessionContext.market == market)
        if account_scope is not None:
            if include_market_wide:
                stmt = stmt.where(
                    sa.or_(
                        OperatorSessionContext.account_scope == account_scope,
                        OperatorSessionContext.account_scope.is_(None),
                    )
                )
            else:
                stmt = stmt.where(OperatorSessionContext.account_scope == account_scope)
        if kst_date_from is not None:
            stmt = stmt.where(OperatorSessionContext.kst_date >= kst_date_from)
        if entry_type is not None:
            stmt = stmt.where(OperatorSessionContext.entry_type == entry_type)
        result = await self._session.scalars(stmt.limit(capped_limit))
        return list(result.all())

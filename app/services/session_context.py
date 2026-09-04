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

    async def has_open_question_for_event_key(self, event_key: str) -> bool:
        """Return whether the durable fill-handoff question already exists."""
        return await self.get_open_question_for_event_key(event_key) is not None

    async def get_open_question_for_event_key(
        self, event_key: str
    ) -> OperatorSessionContext | None:
        """Load the canonical handoff row for narrowly scoped receipt enrichment."""
        stmt = sa.select(OperatorSessionContext).where(
            OperatorSessionContext.entry_type == "open_question",
            OperatorSessionContext.refs.contains({"event_key": event_key}),
        )
        return await self._session.scalar(stmt.limit(1))

    async def append_fill_handoff_kick_result(
        self, *, entry_id: int, flow_run_id: str
    ) -> None:
        """Attach a bounded kickoff receipt to its just-created handoff row.

        This is deliberately not a general edit API: the handoff is durable
        before the best-effort kickoff and the receipt belongs in that same
        operator-visible question.
        """
        row = await self._session.get(OperatorSessionContext, entry_id)
        if row is None or row.created_by != "fill-event-handoff":
            raise ValueError("fill handoff context row unavailable")
        suffix = f"\nPrefect kickoff flow_run_id: {flow_run_id}"
        if suffix not in row.body:
            row.body += suffix
            await self._session.flush()

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

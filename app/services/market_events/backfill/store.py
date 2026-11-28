"""The only writer of ``review.market_event_backfill_requests`` (hk 6409).

INSERT and SELECT only. Every quota decision runs under a transaction-scoped
advisory lock per source, so two operators cannot both pass the same quota.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_event_backfill import MarketEventBackfillRequest
from app.services.market_events.backfill.policy import BackfillRequest

_LOCK_PREFIX = "auto_trader:market_event_backfill:"


class BackfillStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_source(self, source: str) -> datetime:
        """Lock the source until the transaction ends; return the DB clock."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{_LOCK_PREFIX}{source}"},
        )
        # The clock is read after the lock, so the quota day is the day of the
        # decision, not of the transaction start.
        return (await self.session.execute(select(func.clock_timestamp()))).scalar_one()

    async def states_for(self, request_hash: str) -> set[str]:
        model = MarketEventBackfillRequest
        result = await self.session.execute(
            select(model.state).where(model.request_hash == request_hash)
        )
        return set(result.scalars().all())

    async def count(
        self, *, source: str, state: str, since: datetime, until: datetime
    ) -> int:
        model = MarketEventBackfillRequest
        result = await self.session.execute(
            select(func.count())
            .select_from(model)
            .where(
                model.source == source,
                model.state == state,
                model.created_at >= since,
                model.created_at < until,
            )
        )
        return int(result.scalar_one())

    async def insert(
        self,
        request: BackfillRequest,
        *,
        state: str,
        created_at: datetime,
        exit_code: int | None = None,
        failure_code: str | None = None,
    ) -> None:
        self.session.add(
            MarketEventBackfillRequest(
                request_ref=request.request_ref,
                request_hash=request.request_hash,
                source=request.source,
                market=request.market,
                category=request.category,
                symbols=list(request.symbols),
                from_date=request.from_date,
                to_date=request.to_date,
                reason_code=request.reason_code,
                reason=request.reason,
                requested_by=request.requested_by,
                state=state,
                exit_code=exit_code,
                failure_code=failure_code,
                created_at=created_at,
            )
        )
        await self.session.flush()

    async def now(self) -> datetime:
        return (await self.session.execute(select(func.clock_timestamp()))).scalar_one()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

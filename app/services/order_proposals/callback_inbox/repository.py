"""Data access for ``review.telegram_callback_inbox``.

Internal to :mod:`app.services.order_proposals.callback_inbox.service`; an AST
guard (``tests/.../test_repository_boundary.py``) keeps it that way, mirroring
the existing ``order_proposals`` repository boundary.

Like the sibling ``OrderProposalRepository``, this layer flushes and never
commits: transaction boundaries belong to the caller, because the worker's
whole design is about *which* writes land in *which* transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
from app.services.order_proposals.callback_inbox.contracts import (
    MAX_ATTEMPTS,
    TIER_EXHAUSTED,
    TIER_MALFORMED,
    TIER_QUEUED,
    TIER_STALE,
    InboxState,
    recovery_tier_quotas,
)


class CallbackInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, **fields: Any) -> TelegramCallbackInboxJob:
        row = TelegramCallbackInboxJob(**fields)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_job_id(
        self, job_id: uuid.UUID, *, for_update: bool = False
    ) -> TelegramCallbackInboxJob | None:
        stmt = select(TelegramCallbackInboxJob).where(
            TelegramCallbackInboxJob.job_id == job_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_update_digest(
        self, update_digest: str
    ) -> TelegramCallbackInboxJob | None:
        return (
            await self._session.execute(
                select(TelegramCallbackInboxJob).where(
                    TelegramCallbackInboxJob.update_digest == update_digest
                )
            )
        ).scalar_one_or_none()

    async def update(
        self, row: TelegramCallbackInboxJob, **fields: Any
    ) -> TelegramCallbackInboxJob:
        for key, value in fields.items():
            setattr(row, key, value)
        await self._session.flush()
        return row

    async def try_conditional_update(
        self,
        *,
        job_id: uuid.UUID,
        predicate: Any,
        values: dict[str, Any],
    ) -> bool:
        """Apply ``values`` only if the *database* row still matches.

        The precondition is evaluated by PostgreSQL, not against whatever an
        in-memory ORM object happens to remember. That matters because the
        markers these transitions guard on -- ``handler_entered_at`` above all
        -- can be committed by work that happened after this session loaded
        the row, and an in-memory check would cheerfully erase them.
        """
        result = await self._session.execute(
            update(TelegramCallbackInboxJob)
            .where(TelegramCallbackInboxJob.job_id == job_id, predicate)
            .values(**values)
            .returning(TelegramCallbackInboxJob.id)
        )
        updated = result.first() is not None
        if updated:
            # The identity map still holds the pre-update row; drop it so any
            # later read in this session sees what the database now says.
            self._session.expire_all()
        return updated

    async def claimable_job_ids(
        self,
        *,
        now: datetime,
        stale_before: datetime,
        limit: int,
    ) -> list[tuple[uuid.UUID, int]]:
        """Candidates to look at, with the tier each one came from.

        Deliberately returns ids only. Whether a row may actually be claimed
        is decided later, behind the advisory lock, against a freshly read
        row -- this query is a scan filter, not a claim.

        Each tier gets a bounded share of the scan (R29/R34), because no
        single ordering is fair in both directions. Oldest-first alone put
        in-flight work at the front, so a few long-running jobs under live
        worker locks filled every tick and the lost Redis kick behind them was
        never selected. Queued-first inverts it: a backlog bigger than one
        scan and the stale rows are never reached instead. So the tiers are
        ranked and each is fetched separately:

          0. malformed active budget -- a due-independent terminal scrub;
          1. canonical ``retry_wait`` with no attempts left -- R25 cleanup;
          2. canonical ``pending`` and due ``retry_wait`` -- idle work;
          3. canonical stale ``processing`` -- work someone may still be
             doing, and the advisory lock is what settles whether they are.

        Four ordinary ``WHERE ... ORDER BY ... LIMIT`` selects, one per tier,
        rather than one query that ranks the eligible set with a window. The
        window classified and sorted every eligible row, in one query, to
        produce a handful of ids.

        This is a smaller unit of work, not a bounded read. ``EXPLAIN`` on a
        tier query is ``Limit -> Sort(received_at, job_id) -> Index Scan using
        ix_telegram_callback_inbox_state_available``: the predicate uses the
        index, but the ordering still sorts the eligible set, as a
        bounded-memory top-N, because no index matches that order. What is
        genuinely bounded is the number of queries, the rows returned, and the
        memory each sort needs. Making the read stop early would need an index
        built for these predicates and this ordering, which needs its own
        evidence rather than a claim here.

        ``received_at`` then ``job_id`` orders deterministically inside a
        tier: rows arriving in the same instant are ordinary, and without the
        tie-break a tier's share could hold a different subset every tick.
        Everything is computed from the rows themselves, so a process that has
        never swept before sees exactly the same candidates as one that has
        been running all day.
        """
        active = TelegramCallbackInboxJob.state.in_(
            [
                InboxState.PENDING.value,
                InboxState.RETRY_WAIT.value,
                InboxState.PROCESSING.value,
            ]
        )
        # R34: this is intentionally explicit instead of using ``NOT
        # canonical_budget``. SQL NULL would make that negation UNKNOWN, while
        # this tier has to fail closed even for a damaged legacy row whose
        # integer columns somehow became NULL before the current NOT NULL DDL.
        malformed = active & or_(
            TelegramCallbackInboxJob.max_attempts.is_(None),
            TelegramCallbackInboxJob.attempt_count.is_(None),
            TelegramCallbackInboxJob.max_attempts != MAX_ATTEMPTS,
            TelegramCallbackInboxJob.attempt_count < 0,
            TelegramCallbackInboxJob.attempt_count
            > TelegramCallbackInboxJob.max_attempts,
        )
        canonical_budget = and_(
            TelegramCallbackInboxJob.max_attempts == MAX_ATTEMPTS,
            TelegramCallbackInboxJob.attempt_count >= 0,
            TelegramCallbackInboxJob.attempt_count
            <= TelegramCallbackInboxJob.max_attempts,
        )
        due = canonical_budget & (
            TelegramCallbackInboxJob.state.in_(
                [InboxState.PENDING.value, InboxState.RETRY_WAIT.value]
            )
            & (TelegramCallbackInboxJob.available_at <= now)
        )
        # R25: a parked row whose attempt budget is spent is finished, and the
        # backoff must not hide it. ``ck_..._retry_budget`` makes this shape
        # unwritable going forward, but a database written by an older binary
        # can still hold one, and leaving it parked would keep a live nonce
        # and a chat id for the length of the backoff.
        exhausted = (
            canonical_budget
            & (TelegramCallbackInboxJob.state == InboxState.RETRY_WAIT.value)
            & (
                TelegramCallbackInboxJob.attempt_count
                >= TelegramCallbackInboxJob.max_attempts
            )
        )
        stale = (
            canonical_budget
            & (TelegramCallbackInboxJob.state == InboxState.PROCESSING.value)
            & (TelegramCallbackInboxJob.started_at <= stale_before)
        )

        quotas = recovery_tier_quotas(limit)
        predicates = {
            TIER_MALFORMED: malformed,
            TIER_EXHAUSTED: exhausted,
            TIER_QUEUED: due & ~exhausted,
            TIER_STALE: stale,
        }

        candidates: list[tuple[uuid.UUID, int]] = []
        for tier, predicate in sorted(predicates.items()):
            quota = quotas.get(tier, 0)
            if quota <= 0:
                continue
            rows = (
                await self._session.execute(
                    select(TelegramCallbackInboxJob.job_id)
                    .where(predicate)
                    .order_by(
                        TelegramCallbackInboxJob.received_at.asc(),
                        TelegramCallbackInboxJob.job_id.asc(),
                    )
                    .limit(quota)
                )
            ).all()
            candidates.extend((row[0], tier) for row in rows)
        return candidates

    async def counts_by_state(self) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(
                    TelegramCallbackInboxJob.state,
                    func.count(),
                ).group_by(TelegramCallbackInboxJob.state)
            )
        ).all()
        return dict(rows)

    async def oldest_pending_received_at(self) -> datetime | None:
        return (
            await self._session.execute(
                select(func.min(TelegramCallbackInboxJob.received_at)).where(
                    TelegramCallbackInboxJob.state.in_(
                        [InboxState.PENDING.value, InboxState.RETRY_WAIT.value]
                    )
                )
            )
        ).scalar_one_or_none()


__all__ = ["CallbackInboxRepository"]

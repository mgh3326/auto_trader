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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
from app.services.order_proposals.callback_inbox.contracts import InboxState


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
    ) -> list[uuid.UUID]:
        """Job ids the recovery sweep should *look at*, oldest first.

        Deliberately returns ids only. Whether a row may actually be claimed
        is decided later, behind the advisory lock, against a freshly read
        row -- this query is a scan filter, not a claim.
        """
        due = (
            TelegramCallbackInboxJob.state.in_(
                [InboxState.PENDING.value, InboxState.RETRY_WAIT.value]
            )
        ) & (TelegramCallbackInboxJob.available_at <= now)
        # R25: a parked row whose attempt budget is spent is finished, and the
        # backoff must not hide it. ``ck_..._retry_budget`` makes this shape
        # unwritable going forward, but a database written by an older binary
        # can still hold one, and leaving it parked would keep a live nonce
        # and a chat id for the length of the backoff. The classifier already
        # ranks exhaustion above "not yet due"; this is the scan agreeing, so
        # the row actually reaches it. Healthy backoffs are unaffected.
        exhausted = (TelegramCallbackInboxJob.state == InboxState.RETRY_WAIT.value) & (
            TelegramCallbackInboxJob.attempt_count
            >= TelegramCallbackInboxJob.max_attempts
        )
        stale = (TelegramCallbackInboxJob.state == InboxState.PROCESSING.value) & (
            TelegramCallbackInboxJob.started_at <= stale_before
        )
        rows = (
            await self._session.execute(
                select(TelegramCallbackInboxJob.job_id)
                .where(due | stale | exhausted)
                .order_by(TelegramCallbackInboxJob.received_at.asc())
                .limit(limit)
            )
        ).all()
        return [row[0] for row in rows]

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

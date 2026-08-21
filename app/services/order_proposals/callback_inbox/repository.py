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

from sqlalchemy import func, select
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
        stale = (TelegramCallbackInboxJob.state == InboxState.PROCESSING.value) & (
            TelegramCallbackInboxJob.started_at <= stale_before
        )
        rows = (
            await self._session.execute(
                select(TelegramCallbackInboxJob.job_id)
                .where(due | stale)
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

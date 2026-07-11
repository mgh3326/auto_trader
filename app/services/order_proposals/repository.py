"""Internal repository for order_proposals (ROB-816).

INTERNAL ONLY. Imported solely by app/services/order_proposals/service.py
(enforced by tests/services/order_proposals/test_no_repository_imports.py).
Never commits — the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung


class OrderProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_group(self, **cols: Any) -> OrderProposal:
        row = OrderProposal(**cols)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def insert_rung(self, **cols: Any) -> OrderProposalRung:
        row = OrderProposalRung(**cols)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_group_by_proposal_id(
        self, proposal_id: uuid.UUID, *, for_update: bool = False
    ) -> OrderProposal | None:
        stmt = select(OrderProposal).where(OrderProposal.proposal_id == proposal_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_rungs(self, proposal_pk: int) -> list[OrderProposalRung]:
        stmt = (
            select(OrderProposalRung)
            .where(OrderProposalRung.proposal_pk == proposal_pk)
            .order_by(OrderProposalRung.rung_index)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_recent_groups(
        self, *, limit: int, symbol: str | None, lifecycle_state: str | None
    ) -> list[OrderProposal]:
        stmt = select(OrderProposal).order_by(OrderProposal.id.desc()).limit(limit)
        if symbol:
            stmt = stmt.where(OrderProposal.symbol == symbol)
        if lifecycle_state:
            stmt = stmt.where(OrderProposal.lifecycle_state == lifecycle_state)
        return list((await self._session.execute(stmt)).scalars().all())

    async def update_group(self, group: OrderProposal, **fields: Any) -> OrderProposal:
        for k, v in fields.items():
            setattr(group, k, v)
        await self._session.flush()
        return group

    async def update_rung(
        self, rung: OrderProposalRung, **fields: Any
    ) -> OrderProposalRung:
        for k, v in fields.items():
            setattr(rung, k, v)
        await self._session.flush()
        return rung

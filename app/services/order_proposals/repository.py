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
from sqlalchemy.orm.attributes import flag_modified

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

    async def find_rung_by_evidence(
        self,
        *,
        correlation_id: str | None,
        broker_order_id: str | None,
    ) -> tuple[uuid.UUID, OrderProposalRung] | None:
        evidence = (
            (OrderProposalRung.correlation_id, correlation_id),
            (OrderProposalRung.broker_order_id, broker_order_id),
        )
        for column, value in evidence:
            if value is None:
                continue
            stmt = (
                select(OrderProposal.proposal_id, OrderProposalRung)
                .join(
                    OrderProposalRung,
                    OrderProposalRung.proposal_pk == OrderProposal.id,
                )
                .where(column == value)
                .order_by(OrderProposalRung.id)
                .limit(1)
            )
            row = (await self._session.execute(stmt)).one_or_none()
            if row is not None:
                return row[0], row[1]
        return None

    async def list_local_stale_candidates(
        self,
    ) -> list[tuple[uuid.UUID, OrderProposalRung]]:
        stmt = (
            select(OrderProposal.proposal_id, OrderProposalRung)
            .join(
                OrderProposalRung,
                OrderProposalRung.proposal_pk == OrderProposal.id,
            )
            .where(
                OrderProposalRung.state == "pending_approval",
                OrderProposalRung.broker_order_id.is_(None),
            )
            .order_by(OrderProposalRung.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

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
            if k == "updated_at":
                flag_modified(rung, k)
        await self._session.flush()
        return rung

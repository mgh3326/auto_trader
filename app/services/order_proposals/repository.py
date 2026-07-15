"""Internal repository for order_proposals (ROB-816).

INTERNAL ONLY. Imported solely by app/services/order_proposals/service.py
(enforced by tests/services/order_proposals/test_no_repository_imports.py).
Never commits — the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import TIMESTAMP, Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalBatch,
    OrderProposalApprovalBatchMember,
    OrderProposalRung,
)


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

    async def list_groups_by_proposal_prefix(
        self, proposal_prefix: str
    ) -> list[OrderProposal]:
        stmt = (
            select(OrderProposal)
            .where(cast(OrderProposal.proposal_id, Text).like(f"{proposal_prefix}%"))
            .order_by(OrderProposal.id.desc())
            .limit(2)
        )
        return list((await self._session.execute(stmt)).scalars().all())

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

    async def auto_approved_notional_between(
        self,
        *,
        account_mode: str,
        market: str,
        broker_account_id: str | None,
        start: datetime,
        end: datetime,
    ) -> Decimal:
        """Sum rungs belonging to auto-approved groups in a time window."""
        notional = OrderProposalRung.quantity * OrderProposalRung.limit_price
        approved_at = cast(
            OrderProposal.source_asof["auto_approved"]["approved_at"].astext,
            TIMESTAMP(timezone=True),
        )
        stmt = (
            select(func.coalesce(func.sum(notional), 0))
            .select_from(OrderProposal)
            .join(
                OrderProposalRung,
                OrderProposalRung.proposal_pk == OrderProposal.id,
            )
            .where(
                OrderProposal.account_mode == account_mode,
                OrderProposal.market == market,
                approved_at >= start,
                approved_at < end,
                OrderProposal.source_asof.op("?")("auto_approved"),
            )
        )
        if broker_account_id is None:
            stmt = stmt.where(OrderProposal.broker_account_id.is_(None))
        else:
            stmt = stmt.where(OrderProposal.broker_account_id == broker_account_id)
        value = (await self._session.execute(stmt)).scalar_one()
        return Decimal(value)

    async def acquire_auto_approve_lock(self, lock_key: str) -> None:
        """Serialize an auto-approval critical section until transaction commit."""
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )

    async def find_rung_by_evidence(
        self,
        *,
        correlation_id: str | None,
        broker_order_id: str | None,
        idempotency_key: str | None = None,
        states: frozenset[str] | None = None,
        account_mode: str | None = None,
    ) -> tuple[uuid.UUID, OrderProposalRung] | None:
        """Locate a rung by broker or client-order evidence.

        ``states``, when given, restricts the match to rungs currently in one of
        those states. Reconcile passes the evidence-accepting (non-terminal) set
        so that re-delivered evidence for an already-terminal rung simply finds
        nothing (a safe no-op) instead of matching a rung the state machine can
        no longer transition — see OrderProposalsService.record_fill_evidence.
        """
        evidence = (
            (OrderProposalRung.broker_order_id, broker_order_id),
            (OrderProposalRung.idempotency_key, idempotency_key),
            (OrderProposalRung.correlation_id, correlation_id),
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
            )
            if states is not None:
                stmt = stmt.where(OrderProposalRung.state.in_(states))
            if account_mode is not None:
                stmt = stmt.where(OrderProposal.account_mode == account_mode)
            stmt = stmt.order_by(OrderProposalRung.id).limit(1)
            row = (await self._session.execute(stmt)).one_or_none()
            if row is not None:
                return row[0], row[1]
        return None

    # Mirrors service.py's ``_APPROVAL_TERMINAL_GROUP_STATES`` (kept local to
    # avoid the repository importing from the service module it is imported
    # by -- see the module docstring's "INTERNAL ONLY" note). A group in one of
    # these lifecycle_states has already resolved (or been resolved manually)
    # and must not be re-swept just because ``valid_until`` also passed.
    _EXPIRY_TERMINAL_GROUP_STATES = frozenset(
        {"terminal", "rejected", "expired", "voided", "superseded"}
    )

    async def list_expiry_candidates(self, *, now: datetime) -> list[uuid.UUID]:
        """Return proposal_ids for non-terminal groups whose valid_until passed.

        ROB-897 cause (1): ``expire_if_needed`` only ever ran from the Telegram
        approval callback, so a proposal nobody tapped stayed
        ``proposed``/``needs_reconfirm`` forever past its deadline. This is the
        candidate finder for the batch sweeper (``OrderProposalsService.sweep_expired``).
        """
        stmt = (
            select(OrderProposal.proposal_id)
            .where(
                OrderProposal.valid_until.is_not(None),
                OrderProposal.valid_until <= now,
                OrderProposal.lifecycle_state.not_in(
                    self._EXPIRY_TERMINAL_GROUP_STATES
                ),
            )
            .order_by(OrderProposal.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

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

    async def acquire_approval_batch_chat_lock(self, chat_id: str) -> None:
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"order_proposals:approval_batch:{chat_id}", 0
                    )
                )
            )
        )

    async def get_open_approval_batch(
        self, *, chat_id: str, now: datetime, for_update: bool = False
    ) -> OrderProposalApprovalBatch | None:
        stmt = (
            select(OrderProposalApprovalBatch)
            .where(
                OrderProposalApprovalBatch.chat_id == chat_id,
                OrderProposalApprovalBatch.approval_nonce_used_at.is_(None),
                OrderProposalApprovalBatch.window_closes_at > now,
                OrderProposalApprovalBatch.expires_at > now,
            )
            .order_by(OrderProposalApprovalBatch.id.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def insert_approval_batch(self, **cols: Any) -> OrderProposalApprovalBatch:
        row = OrderProposalApprovalBatch(**cols)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def insert_approval_batch_member(
        self, **cols: Any
    ) -> OrderProposalApprovalBatchMember:
        row = OrderProposalApprovalBatchMember(**cols)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_approval_batch_member_by_nonce(
        self, *, proposal_pk: int, approval_nonce: str
    ) -> OrderProposalApprovalBatchMember | None:
        stmt = select(OrderProposalApprovalBatchMember).where(
            OrderProposalApprovalBatchMember.proposal_pk == proposal_pk,
            OrderProposalApprovalBatchMember.approval_nonce_snapshot == approval_nonce,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_approval_batch_member_by_id(
        self, member_id: int, *, for_update: bool = False
    ) -> OrderProposalApprovalBatchMember | None:
        stmt = select(OrderProposalApprovalBatchMember).where(
            OrderProposalApprovalBatchMember.id == member_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_approval_batch_members(
        self, batch_pk: int
    ) -> list[OrderProposalApprovalBatchMember]:
        stmt = (
            select(OrderProposalApprovalBatchMember)
            .where(OrderProposalApprovalBatchMember.batch_pk == batch_pk)
            .order_by(
                OrderProposalApprovalBatchMember.added_at,
                OrderProposalApprovalBatchMember.id,
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_group_by_pk(self, proposal_pk: int) -> OrderProposal | None:
        stmt = select(OrderProposal).where(OrderProposal.id == proposal_pk)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_approval_batch_by_id(
        self, batch_id: uuid.UUID, *, for_update: bool = False
    ) -> OrderProposalApprovalBatch | None:
        stmt = select(OrderProposalApprovalBatch).where(
            OrderProposalApprovalBatch.batch_id == batch_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def resolve_approval_batch_id_prefix(
        self, batch_short: str
    ) -> uuid.UUID | None:
        stmt = (
            select(OrderProposalApprovalBatch.batch_id)
            .where(
                cast(OrderProposalApprovalBatch.batch_id, Text).like(f"{batch_short}%")
            )
            .limit(2)
        )
        matches = list((await self._session.execute(stmt)).scalars().all())
        return matches[0] if len(matches) == 1 else None

    async def update_approval_batch(
        self, batch: OrderProposalApprovalBatch, **fields: Any
    ) -> OrderProposalApprovalBatch:
        for key, value in fields.items():
            setattr(batch, key, value)
        await self._session.flush()
        return batch

    async def update_approval_batch_member(
        self, member: OrderProposalApprovalBatchMember, **fields: Any
    ) -> OrderProposalApprovalBatchMember:
        for key, value in fields.items():
            setattr(member, key, value)
        await self._session.flush()
        return member

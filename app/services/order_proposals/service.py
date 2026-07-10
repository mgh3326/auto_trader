"""OrderProposalsService — the ONLY writer surface for order_proposals (ROB-816).

Sessions are constructor-injected; this service flush()es (via the repository)
and never commits — callers own the transaction (see global-constraints.md).
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
)
from app.services.order_proposals.payload import (
    ProposalRungSpec,
    compute_proposal_payload_hash,
)
from app.services.order_proposals.repository import OrderProposalRepository


@dataclass
class RungInput:
    rung_index: int
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    notional: Decimal | None


class OrderProposalsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = OrderProposalRepository(session)

    async def create_proposal(
        self,
        *,
        symbol: str,
        market: str,
        account_mode: str,
        side: str,
        order_type: str,
        proposer: str,
        rungs: list[RungInput],
        thesis: str | None = None,
        strategy: str | None = None,
        rationale: dict | None = None,
        broker_account_id: str | None = None,
        lot_context: dict | None = None,
        valid_until: datetime | None = None,
        correlation_id: str | None = None,
        source_asof: dict | None = None,
        supersedes_proposal_id: uuid.UUID | None = None,
    ) -> OrderProposal:
        if not rungs:
            raise ValueError("at least one rung required")
        proposal_id = uuid.uuid4()
        root_id = proposal_id
        superseded_group: OrderProposal | None = None
        if supersedes_proposal_id is not None:
            superseded_group = await self._repo.get_group_by_proposal_id(
                supersedes_proposal_id, for_update=True
            )
            if superseded_group is None:
                raise OrderProposalNotFound(str(supersedes_proposal_id))
            root_id = superseded_group.root_proposal_id

        payload_hash = compute_proposal_payload_hash(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            order_type=order_type,
            rungs=[
                ProposalRungSpec(
                    r.rung_index,
                    r.side,
                    str(r.quantity),
                    None if r.limit_price is None else str(r.limit_price),
                    None if r.notional is None else str(r.notional),
                )
                for r in rungs
            ],
        )

        group = await self._repo.insert_group(
            proposal_id=proposal_id,
            root_proposal_id=root_id,
            revision=1,
            supersedes_proposal_id=supersedes_proposal_id,
            no_resubmit=False,
            payload_hash=payload_hash,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side=side,
            order_type=order_type,
            proposer=proposer,
            thesis=thesis,
            strategy=strategy,
            rationale=rationale,
            broker_account_id=broker_account_id,
            lot_context=lot_context,
            lifecycle_state="proposed",
            correlation_id=correlation_id,
            valid_until=valid_until,
            source_asof=source_asof,
        )
        for r in rungs:
            await self._repo.insert_rung(
                proposal_pk=group.id,
                rung_index=r.rung_index,
                side=r.side,
                quantity=r.quantity,
                limit_price=r.limit_price,
                notional=r.notional,
                state="pending_approval",
            )
        if superseded_group is not None:
            await self._repo.update_group(
                superseded_group,
                lifecycle_state="superseded",
                superseded_by_proposal_id=proposal_id,
            )
        return group

    async def get_proposal(
        self, proposal_id: uuid.UUID
    ) -> tuple[OrderProposal, list[OrderProposalRung]]:
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        return group, rungs

    async def list_recent(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
        lifecycle_state: str | None = None,
    ) -> list[tuple[OrderProposal, list[OrderProposalRung]]]:
        groups = await self._repo.list_recent_groups(
            limit=limit, symbol=symbol, lifecycle_state=lifecycle_state
        )
        return [(g, await self._repo.list_rungs(g.id)) for g in groups]

    async def transition_rung(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        new_state: str,
        **audit_fields: Any,
    ) -> OrderProposalRung:
        group, rung = await self._get_locked_rung(proposal_id, rung_index)
        return await self._transition_locked_rung(
            group, rung, new_state=new_state, **audit_fields
        )

    async def _get_locked_rung(
        self, proposal_id: uuid.UUID, rung_index: int
    ) -> tuple[OrderProposal, OrderProposalRung]:
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        rung = next((r for r in rungs if r.rung_index == rung_index), None)
        if rung is None:
            raise OrderProposalNotFound(f"{proposal_id}#{rung_index}")
        return group, rung

    async def _transition_locked_rung(
        self,
        group: OrderProposal,
        rung: OrderProposalRung,
        *,
        new_state: str,
        **audit_fields: Any,
    ) -> OrderProposalRung:
        sm.assert_rung_transition(rung.state, new_state)
        rung = await self._repo.update_rung(rung, state=new_state, **audit_fields)
        rungs = await self._repo.list_rungs(group.id)
        await self._repo.update_group(
            group, lifecycle_state=self._recompute_group_state(rungs)
        )
        return rung

    @staticmethod
    def _recompute_group_state(rungs: list[OrderProposalRung]) -> str:
        states = {r.state for r in rungs}
        if states <= {
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "voided",
            "voided_local_stale",
            "superseded",
        }:
            if states == {"rejected"}:
                return "rejected"
            if states <= {"voided", "voided_local_stale"}:
                return "voided"
            return "terminal"
        if states & {"acked", "resting", "partially_filled", "filled", "submitting"}:
            if states & {
                "pending_approval",
                "revalidating",
                "approved",
                "needs_reconfirm",
            }:
                return "partially_submitted"
            return "submitted"
        if states & {"approved"}:
            return "approved"
        return "proposed"

    # -- PR-2 helpers -------------------------------------------------------
    async def set_approval_nonce(self, proposal_id: uuid.UUID, nonce: str) -> None:
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        await self._repo.update_group(
            group, approval_nonce=nonce, approval_nonce_used_at=None
        )

    async def consume_approval_nonce(
        self, proposal_id: uuid.UUID, nonce: str, *, now: datetime
    ) -> OrderProposal:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        if group.approval_nonce != nonce:
            raise OrderProposalError("nonce_mismatch")
        if group.approval_nonce_used_at is not None:
            raise OrderProposalError("nonce_replay")
        return await self._repo.update_group(group, approval_nonce_used_at=now)

    async def acquire_commit_lease(
        self,
        proposal_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int = 10,
    ) -> bool:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        lease_until = group.commit_lease_until
        if lease_until is not None:
            self._require_timezone_aware(lease_until)
            if lease_until > now:
                return False
        await self._repo.update_group(
            group, commit_lease_until=now + timedelta(seconds=lease_seconds)
        )
        return True

    async def record_ack(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        broker_order_id: str,
        correlation_id: str,
        idempotency_key: str,
        approval_hash_digest: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="acked",
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            approval_hash_digest=approval_hash_digest,
            validated_at=now,
            updated_at=now,
        )

    async def record_resting(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        broker_order_id: str,
        correlation_id: str,
        idempotency_key: str,
        approval_hash_digest: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="resting",
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            approval_hash_digest=approval_hash_digest,
            validated_at=now,
            updated_at=now,
        )

    async def record_unverified(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        reason: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="unverified",
            void_reason=reason,
            validated_at=now,
            updated_at=now,
        )

    async def record_fill_evidence(
        self,
        *,
        correlation_id: str | None = None,
        broker_order_id: str | None = None,
        filled_qty: Decimal,
        terminal_state: Literal["filled", "partially_filled"] = "filled",
        now: datetime,
    ) -> OrderProposalRung | None:
        self._require_timezone_aware(now)
        match = await self._repo.find_rung_by_evidence(
            correlation_id=correlation_id, broker_order_id=broker_order_id
        )
        if match is None:
            return None
        proposal_id, rung = match
        return await self.transition_rung(
            proposal_id,
            rung.rung_index,
            new_state=terminal_state,
            filled_qty=filled_qty,
            updated_at=now,
        )

    async def mark_needs_reconfirm(
        self, proposal_id: uuid.UUID, rung_index: int, *, now: datetime
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        group, rung = await self._get_locked_rung(proposal_id, rung_index)
        return await self._transition_locked_rung(
            group,
            rung,
            new_state="needs_reconfirm",
            approval_revision=(rung.approval_revision or 0) + 1,
            validated_at=now,
            updated_at=now,
        )

    async def record_rejected(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        reason: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="rejected",
            void_reason=reason,
            updated_at=now,
        )

    async def sweep_local_stale(
        self,
        *,
        now: datetime,
        broker_evidence: Callable[[OrderProposalRung], str | Awaitable[str]],
    ) -> list[uuid.UUID]:
        self._require_timezone_aware(now)
        candidates = await self._repo.list_local_stale_candidates()
        swept: list[uuid.UUID] = []
        swept_set: set[uuid.UUID] = set()
        for proposal_id, candidate in candidates:
            evidence = broker_evidence(candidate)
            if inspect.isawaitable(evidence):
                evidence = await evidence
            if evidence != "no_broker_order":
                continue

            group, rung = await self._get_locked_rung(proposal_id, candidate.rung_index)
            if rung.state != "pending_approval" or rung.broker_order_id is not None:
                continue
            await self._transition_locked_rung(
                group,
                rung,
                new_state="voided_local_stale",
                void_reason="no_broker_order",
                updated_at=now,
            )
            if proposal_id not in swept_set:
                swept.append(proposal_id)
                swept_set.add(proposal_id)
        return swept

    @staticmethod
    def _require_timezone_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")

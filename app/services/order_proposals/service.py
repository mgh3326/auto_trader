"""OrderProposalsService — the ONLY writer surface for order_proposals (ROB-816).

Sessions are constructor-injected; this service flush()es (via the repository)
and never commits — callers own the transaction (see global-constraints.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.errors import OrderProposalNotFound
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
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        rung = next((r for r in rungs if r.rung_index == rung_index), None)
        if rung is None:
            raise OrderProposalNotFound(f"{proposal_id}#{rung_index}")
        sm.assert_rung_transition(rung.state, new_state)
        rung = await self._repo.update_rung(rung, state=new_state, **audit_fields)
        # refresh rung list then recompute group rollup
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
    # Stubs only. Task 12 (PR 2) fills these in with the Telegram approval
    # flow, callback-nonce binding, commit-lease, and fill-evidence recording.
    # Deliberately unimplemented in PR 1 (no Telegram, no broker mutation).

    async def set_approval_nonce(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("set_approval_nonce is implemented in PR 2 (ROB-816)")

    async def consume_approval_nonce(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "consume_approval_nonce is implemented in PR 2 (ROB-816)"
        )

    async def acquire_commit_lease(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "acquire_commit_lease is implemented in PR 2 (ROB-816)"
        )

    async def record_ack(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("record_ack is implemented in PR 2 (ROB-816)")

    async def record_resting(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("record_resting is implemented in PR 2 (ROB-816)")

    async def record_unverified(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("record_unverified is implemented in PR 2 (ROB-816)")

    async def record_fill_evidence(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "record_fill_evidence is implemented in PR 2 (ROB-816)"
        )

    async def sweep_local_stale(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("sweep_local_stale is implemented in PR 2 (ROB-816)")

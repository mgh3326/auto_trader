"""PostgreSQL ports for the bounded Kiwoom coordination owner.

There is no distributed transaction spanning a Kiwoom HTTP acknowledgement
and PostgreSQL.  The safety boundary is therefore write-ahead, not fictional
atomicity: ``review.order_send_intents`` is committed before broker I/O.  The
post-dispatch write below stores the acknowledged lineage and typed dispatch
evidence in one database transaction.  If every post-ACK write fails, the
pre-send claim still survives and restart discovery classifies it as missing
evidence, blocking replay until authoritative reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiwoom_coordination_lifecycle import KiwoomCoordinationLifecycle
from app.models.review import OrderSendIntent
from app.services.mock_integration.coordination import (
    DispatchEvidence,
    DispatchEvidenceKind,
    DurableSendClaimAdapter,
    MutationCertainty,
)
from app.services.mock_integration.lineage import LineageEnvelope
from app.services.order_send_intent_service import (
    OrderSendIntentReservation,
    OrderSendIntentService,
)

type AsyncSessionFactory = Callable[[], AsyncSession]


class KiwoomCoordinationStoreConflict(RuntimeError):
    """A durable row disagrees with the immutable lineage being persisted."""


class KiwoomRestartClaimState(StrEnum):
    """Restart classification derived only from committed database rows."""

    EVIDENCE_MISSING = "evidence_missing"
    UNCERTAIN = "uncertain"
    DEFINITIVE_TRACKED = "definitive_tracked"


@dataclass(frozen=True, slots=True)
class RediscoveredKiwoomClaim:
    """One unreleased binary claim reconstructed after process restart."""

    row_id: int
    claim_account_scope: str
    idempotency_key: str
    side: str | None
    state: KiwoomRestartClaimState
    dispatch_kind: DispatchEvidenceKind | None
    certainty: MutationCertainty | None
    broker_order_id: str | None
    ack_envelope_recorded: bool

    @property
    def blocks_account(self) -> bool:
        """Missing or explicitly uncertain evidence fail-closes the account."""

        return self.state in {
            KiwoomRestartClaimState.EVIDENCE_MISSING,
            KiwoomRestartClaimState.UNCERTAIN,
        }


def _envelope_parts(
    envelope: LineageEnvelope,
) -> tuple[Any, Any, dict[str, Any]]:
    if type(envelope) is not LineageEnvelope:
        raise KiwoomCoordinationStoreConflict("lineage_envelope_type_rejected")
    execution_plan = envelope.execution_plan
    order_attempt = envelope.order_attempt
    if execution_plan is None or order_attempt is None:
        raise KiwoomCoordinationStoreConflict("attempt_lineage_required")
    return execution_plan, order_attempt, envelope.model_dump(mode="json")


class KiwoomOrderSendIntentPort:
    """Fresh-session adapter over the existing binary reservation service."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        *,
        account_scope: str,
        idempotency_key: str,
        symbol: str | None = None,
        side: str | None = None,
        conflicting_key_sides: tuple[tuple[str, str], ...] = (),
    ) -> int:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).reserve(
                account_scope=account_scope,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
                conflicting_key_sides=conflicting_key_sides,
            )

    async def list_reservations(
        self, *, account_scope: str
    ) -> Sequence[OrderSendIntentReservation]:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).list_reservations(
                account_scope=account_scope
            )

    async def release_if_matches(
        self,
        *,
        account_scope: str,
        row_id: int,
        idempotency_key: str,
        side: str | None,
    ) -> int:
        async with self._session_factory() as session:
            return await OrderSendIntentService(session).release_if_matches(
                account_scope=account_scope,
                row_id=row_id,
                idempotency_key=idempotency_key,
                side=side,
            )


class KiwoomCoordinationStore:
    """Implements lineage, dispatch-evidence, uncertainty, and restart reads."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        lane_id: str,
        physical_account_id: str,
        claim_account_scope: str,
    ) -> None:
        if lane_id != "kr.kiwoom.mock":
            raise KiwoomCoordinationStoreConflict("kiwoom_lane_rejected")
        if not physical_account_id.strip() or not claim_account_scope.strip():
            raise KiwoomCoordinationStoreConflict("kiwoom_account_scope_rejected")
        self._session_factory = session_factory
        self._lane_id = lane_id
        self._physical_account_id = physical_account_id
        self._claim_account_scope = claim_account_scope

    def _validate_plan(self, execution_plan: Any) -> None:
        if (
            execution_plan.lane_id != self._lane_id
            or execution_plan.broker != "kiwoom"
            or execution_plan.account_profile != "mock"
            or execution_plan.account_mode != "mock"
        ):
            raise KiwoomCoordinationStoreConflict("kiwoom_lineage_lane_mismatch")

    def _validate_existing(
        self,
        row: KiwoomCoordinationLifecycle,
        *,
        envelope: LineageEnvelope,
    ) -> tuple[Any, Any, dict[str, Any]]:
        execution_plan, order_attempt, payload = _envelope_parts(envelope)
        self._validate_plan(execution_plan)
        if (
            row.lane_id != self._lane_id
            or row.physical_account_id != self._physical_account_id
            or row.claim_account_scope != self._claim_account_scope
            or row.decision_intent_id != envelope.decision_intent.decision_intent_id
            or row.execution_plan_id != execution_plan.execution_plan_id
            or row.order_attempt_id != order_attempt.order_attempt_id
            or row.idempotency_key != order_attempt.idempotency_key
        ):
            raise KiwoomCoordinationStoreConflict("kiwoom_lineage_conflict")
        return execution_plan, order_attempt, payload

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        """Commit the pre-send lineage or its later immutable ACK attachment."""

        execution_plan, order_attempt, payload = _envelope_parts(envelope)
        self._validate_plan(execution_plan)
        broker_order_id = order_attempt.broker_order_id
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KiwoomCoordinationLifecycle)
                .where(
                    KiwoomCoordinationLifecycle.claim_account_scope
                    == self._claim_account_scope,
                    KiwoomCoordinationLifecycle.idempotency_key
                    == order_attempt.idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                if broker_order_id is not None:
                    raise KiwoomCoordinationStoreConflict(
                        "pre_send_lineage_missing_before_ack"
                    )
                session.add(
                    KiwoomCoordinationLifecycle(
                        lane_id=self._lane_id,
                        physical_account_id=self._physical_account_id,
                        claim_account_scope=self._claim_account_scope,
                        decision_intent_id=envelope.decision_intent.decision_intent_id,
                        execution_plan_id=execution_plan.execution_plan_id,
                        order_attempt_id=order_attempt.order_attempt_id,
                        idempotency_key=order_attempt.idempotency_key,
                        initial_envelope=payload,
                    )
                )
            else:
                _, _, payload = self._validate_existing(row, envelope=envelope)
                if broker_order_id is None:
                    if row.initial_envelope != payload:
                        raise KiwoomCoordinationStoreConflict(
                            "initial_lineage_immutable"
                        )
                elif row.ack_envelope is None:
                    if row.broker_order_id not in (None, broker_order_id):
                        raise KiwoomCoordinationStoreConflict(
                            "broker_order_id_conflict"
                        )
                    row.ack_envelope = payload
                    row.broker_order_id = broker_order_id
                elif (
                    row.ack_envelope != payload
                    or row.broker_order_id != broker_order_id
                ):
                    raise KiwoomCoordinationStoreConflict("ack_lineage_immutable")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KiwoomCoordinationStoreConflict(
                    "kiwoom_lineage_conflict"
                ) from exc

    async def persist_dispatch_evidence(self, evidence: DispatchEvidence, /) -> None:
        """Atomically persist the ACK envelope and typed dispatch evidence."""

        if type(evidence) is not DispatchEvidence:
            raise KiwoomCoordinationStoreConflict("dispatch_evidence_type_rejected")
        if (
            type(evidence.kind) is not DispatchEvidenceKind
            or type(evidence.certainty) is not MutationCertainty
            or type(evidence.claim_row_id) is not int
            or any(
                type(value) is not bool
                for value in (
                    evidence.callback_failed,
                    evidence.ack_attachment_failed,
                    evidence.outer_cancellation_requested,
                )
            )
            or (
                evidence.broker_order_id is not None
                and (
                    type(evidence.broker_order_id) is not str
                    or not evidence.broker_order_id.strip()
                )
            )
        ):
            raise KiwoomCoordinationStoreConflict("dispatch_evidence_shape_rejected")
        execution_plan, order_attempt, payload = _envelope_parts(evidence.envelope)
        self._validate_plan(execution_plan)
        if (
            evidence.claim_account_scope != self._claim_account_scope
            or evidence.decision_intent_id
            != evidence.envelope.decision_intent.decision_intent_id
            or evidence.execution_plan_id != execution_plan.execution_plan_id
            or evidence.order_attempt_id != order_attempt.order_attempt_id
            or evidence.idempotency_key != order_attempt.idempotency_key
        ):
            raise KiwoomCoordinationStoreConflict("dispatch_lineage_mismatch")

        async with self._session_factory() as session:
            row = await session.scalar(
                select(KiwoomCoordinationLifecycle)
                .where(
                    KiwoomCoordinationLifecycle.claim_account_scope
                    == self._claim_account_scope,
                    KiwoomCoordinationLifecycle.idempotency_key
                    == evidence.idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                raise KiwoomCoordinationStoreConflict("pre_send_lineage_missing")
            self._validate_existing(row, envelope=evidence.envelope)
            claim = await session.scalar(
                select(OrderSendIntent).where(
                    OrderSendIntent.id == evidence.claim_row_id,
                    OrderSendIntent.account_scope == self._claim_account_scope,
                    OrderSendIntent.idempotency_key == evidence.idempotency_key,
                )
            )
            if claim is None or (
                claim.side is not None
                and claim.side != evidence.envelope.decision_intent.side
            ):
                raise KiwoomCoordinationStoreConflict("dispatch_claim_mismatch")

            projection = (
                payload,
                evidence.kind.value,
                evidence.certainty.value,
                evidence.claim_row_id,
                evidence.callback_failed,
                evidence.ack_attachment_failed,
                evidence.outer_cancellation_requested,
                evidence.broker_order_id,
            )
            existing_projection = (
                row.dispatch_envelope,
                row.dispatch_kind,
                row.mutation_certainty,
                row.claim_row_id,
                row.callback_failed,
                row.ack_attachment_failed,
                row.outer_cancellation_requested,
                row.broker_order_id,
            )
            if row.dispatch_kind is not None:
                if existing_projection != projection:
                    raise KiwoomCoordinationStoreConflict("dispatch_evidence_immutable")
                return

            if (
                row.broker_order_id is not None
                and evidence.broker_order_id is not None
                and row.broker_order_id != evidence.broker_order_id
            ):
                raise KiwoomCoordinationStoreConflict("broker_order_id_conflict")
            row.dispatch_envelope = payload
            row.dispatch_kind = evidence.kind.value
            row.mutation_certainty = evidence.certainty.value
            row.claim_row_id = evidence.claim_row_id
            row.callback_failed = evidence.callback_failed
            row.ack_attachment_failed = evidence.ack_attachment_failed
            row.outer_cancellation_requested = evidence.outer_cancellation_requested
            if evidence.broker_order_id is not None:
                row.broker_order_id = evidence.broker_order_id
            if order_attempt.broker_order_id is not None:
                if row.broker_order_id != order_attempt.broker_order_id:
                    raise KiwoomCoordinationStoreConflict("broker_order_id_conflict")
                if row.ack_envelope is None:
                    row.ack_envelope = payload
                elif row.ack_envelope != payload:
                    raise KiwoomCoordinationStoreConflict("ack_lineage_immutable")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KiwoomCoordinationStoreConflict(
                    "dispatch_evidence_conflict"
                ) from exc

    async def rediscover_unreleased_claims(
        self,
    ) -> tuple[RediscoveredKiwoomClaim, ...]:
        """Left-join every surviving claim to evidence in a fresh session."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(OrderSendIntent, KiwoomCoordinationLifecycle)
                .outerjoin(
                    KiwoomCoordinationLifecycle,
                    and_(
                        KiwoomCoordinationLifecycle.claim_account_scope
                        == OrderSendIntent.account_scope,
                        KiwoomCoordinationLifecycle.idempotency_key
                        == OrderSendIntent.idempotency_key,
                    ),
                )
                .where(OrderSendIntent.account_scope == self._claim_account_scope)
                .order_by(OrderSendIntent.id)
            )
            discovered: list[RediscoveredKiwoomClaim] = []
            for claim, lifecycle in result.all():
                kind = (
                    None
                    if lifecycle is None or lifecycle.dispatch_kind is None
                    else DispatchEvidenceKind(lifecycle.dispatch_kind)
                )
                certainty = (
                    None
                    if lifecycle is None or lifecycle.mutation_certainty is None
                    else MutationCertainty(lifecycle.mutation_certainty)
                )
                if kind is None or certainty is None:
                    state = KiwoomRestartClaimState.EVIDENCE_MISSING
                elif certainty is MutationCertainty.UNCERTAIN or kind in {
                    DispatchEvidenceKind.LANE_REPORTED_UNCERTAIN,
                    DispatchEvidenceKind.CALLBACK_FAILED,
                    DispatchEvidenceKind.ACK_ATTACHMENT_FAILED,
                }:
                    state = KiwoomRestartClaimState.UNCERTAIN
                else:
                    state = KiwoomRestartClaimState.DEFINITIVE_TRACKED
                discovered.append(
                    RediscoveredKiwoomClaim(
                        row_id=claim.id,
                        claim_account_scope=claim.account_scope,
                        idempotency_key=claim.idempotency_key,
                        side=claim.side,
                        state=state,
                        dispatch_kind=kind,
                        certainty=certainty,
                        broker_order_id=(
                            None if lifecycle is None else lifecycle.broker_order_id
                        ),
                        ack_envelope_recorded=(
                            lifecycle is not None and lifecycle.ack_envelope is not None
                        ),
                    )
                )
            return tuple(discovered)

    async def has_unresolved_account_uncertainty(
        self, *, claim_account_scope: str
    ) -> bool:
        """Rediscover durable claims under the lease and fail closed on gaps."""

        if claim_account_scope != self._claim_account_scope:
            raise KiwoomCoordinationStoreConflict("claim_account_scope_mismatch")
        return any(
            claim.blocks_account for claim in await self.rediscover_unreleased_claims()
        )


class KiwoomDurableSendClaimAdapter(DurableSendClaimAdapter):
    """J2B claims plus the lane-owned restart rediscovery operation."""

    __slots__ = ("_store",)

    def __init__(
        self,
        intents: KiwoomOrderSendIntentPort,
        *,
        store: KiwoomCoordinationStore,
    ) -> None:
        super().__init__(intents)
        self._store = store

    async def rediscover_unreleased_claims(
        self,
    ) -> tuple[RediscoveredKiwoomClaim, ...]:
        """Recovery owner entrypoint used after constructing fresh ports."""

        return await self._store.rediscover_unreleased_claims()


__all__ = [
    "KiwoomCoordinationStore",
    "KiwoomCoordinationStoreConflict",
    "KiwoomDurableSendClaimAdapter",
    "KiwoomOrderSendIntentPort",
    "KiwoomRestartClaimState",
    "RediscoveredKiwoomClaim",
]

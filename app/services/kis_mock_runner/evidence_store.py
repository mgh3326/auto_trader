"""Durable KR mock ports; never a broker client or an automatic recovery worker."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Column,
    MetaData,
    Table,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kis_mock_dispatch_evidence import (
    KISMockDispatchEvidence,
    KISMockDispatchLineage,
)
from app.models.review import OrderSendIntent
from app.services.kis_mock_runner.position_proof import (
    MAX_BASELINE_AGE,
    BalanceSnapshot,
    KISMockPositionReader,
    PositionProof,
    PositionUnproven,
    expected_quantity,
)
from app.services.kis_mock_runner.registry_binding import require_kis_mock_identity
from app.services.kis_mock_runner.singleton import (
    KIS_MOCK_LANE_EVIDENCE_KINDS,
    KISMockLanePorts,
)
from app.services.mock_integration.coordination import (
    DispatchEvidence,
    DispatchEvidenceKind,
    DurableClaim,
    DurableSendClaimAdapter,
    MutationCertainty,
    TerminalClaimEvidence,
    physical_account_scope_for_entry,
)
from app.services.mock_integration.lineage import LineageEnvelope
from app.services.mock_lane_registry import LaneRegistryEntry

_DISPATCH_EVIDENCE_KEY = "dispatch"
_ACK_ENVELOPE_KEY = "lineage-ack"
_TERMINAL_FILL_KINDS = ("terminal_reconciliation", "cancel", "expiry", "reject")
_KST = timezone(timedelta(hours=9))
_POSITION_BASELINE = Table(
    "kis_mock_position_baseline",
    MetaData(),
    Column("order_attempt_id", Text, primary_key=True),
    Column("symbol", Text, nullable=False),
    Column("quantity", BigInteger, nullable=False),
    Column("read_digest", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True)),
    schema="review",
)


class KISMockEvidenceConflict(ValueError):
    """A closed-shape evidence or exact identity check failed (no raw values)."""


@dataclass(frozen=True, slots=True)
class RediscoveredKISMockClaim:
    claim: DurableClaim
    order_attempt_id: str | None
    broker_order_id: str | None
    blocks_account: bool
    attribution_error: str | None = None


@dataclass(frozen=True, slots=True)
class KISMockRecoveryLineage:
    envelope: LineageEnvelope
    # Claim creation precedes the broker send and pins its KST day when a
    # matching, post-send ACK was committed on that same day.
    created_at: datetime
    acknowledged_at: datetime


@dataclass(frozen=True, slots=True)
class NativeOrderEvidence:
    """An operator reconciler's exact-order result, with no raw broker payload.

    Evidence producers must use authoritative broker readback, never local-clock
    expiry, symbol-level holdings attribution, or a soft/local cancellation.
    Recording this object grants no mutation or release authority by itself.
    """

    kind: str
    broker_evidence_key: str
    broker_order_id: str | None
    readback_order_id: str | None = None
    terminal: TerminalClaimEvidence = TerminalClaimEvidence()
    filled_quantity: int | None = None
    position_proof: PositionProof | None = None


def _key(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise KISMockEvidenceConflict("broker_evidence_key_required")
    return hashlib.sha256(b"kis-mock-evidence-v1\0" + value.encode()).hexdigest()


def _dispatch_matches_claim(event: KISMockDispatchEvidence, claim_row_id: int) -> bool:
    payload = event.payload
    return (
        event.broker_evidence_key == _key(_DISPATCH_EVIDENCE_KEY)
        and event.kind in ("ack", "unknown")
        and type(claim_row_id) is int
        and claim_row_id > 0
        and type(payload) is dict
        and type(payload.get("claim_row_id")) is int
        and payload["claim_row_id"] == claim_row_id
    )


def _payload(envelope: LineageEnvelope) -> dict[str, Any]:
    if type(envelope) is not LineageEnvelope:
        raise KISMockEvidenceConflict("lineage_type_rejected")
    envelope = LineageEnvelope.model_validate(envelope.model_dump())
    plan, attempt = envelope.execution_plan, envelope.order_attempt
    if (
        plan is None
        or attempt is None
        or (plan.lane_id, plan.broker, plan.account_profile, plan.account_mode)
        != ("kr.kis.mock", "kis", "mock", "mock")
    ):
        raise KISMockEvidenceConflict("kr_kis_mock_lineage_required")
    return envelope.model_dump(mode="json")


class KISMockEvidenceStore:
    """LineagePersistencePort, DispatchEvidencePort and account uncertainty gate.

    Fresh sessions commit before returning. Inserts use unique conflict targets
    and compare immutable payloads, so concurrent identical writers converge;
    a reused key with different evidence refuses rather than overwriting.
    """

    def __init__(
        self, session_factory: Callable[[], AsyncSession], *, entry: LaneRegistryEntry
    ) -> None:
        require_kis_mock_identity(entry)
        self._session_factory = session_factory
        self._entry = entry
        self._identity = entry.physical_account_id
        self._scope = physical_account_scope_for_entry(entry).claim_account_scope

    def position_reader(self) -> KISMockPositionReader:
        return KISMockPositionReader(self._entry)

    def ports(self) -> KISMockLanePorts:
        return KISMockLanePorts(
            persistence=self,
            dispatch_evidence=self,
            uncertainty_gate=self,
            evidence_kinds=KIS_MOCK_LANE_EVIDENCE_KINDS,
        )

    async def _lineage(
        self, session: AsyncSession, envelope: LineageEnvelope
    ) -> KISMockDispatchLineage:
        payload = _payload(envelope)
        attempt = payload["order_attempt"]
        row = await session.get(
            KISMockDispatchLineage, attempt["order_attempt_id"], with_for_update=True
        )
        initial = {**payload, "order_attempt": {**attempt, "broker_order_id": None}}
        if (
            row is None
            or row.physical_account_id != self._identity
            or row.claim_account_scope != self._scope
            or row.initial_envelope != initial
        ):
            raise KISMockEvidenceConflict("lineage_binding_mismatch")
        return row

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        payload = _payload(envelope)
        attempt = payload["order_attempt"]
        async with self._session_factory() as session:
            if attempt["broker_order_id"] is None:
                values = {
                    "order_attempt_id": attempt["order_attempt_id"],
                    "lane_id": "kr.kis.mock",
                    "physical_account_id": self._identity,
                    "claim_account_scope": self._scope,
                    "idempotency_key": attempt["idempotency_key"],
                    "initial_envelope": payload,
                }
                await session.execute(
                    insert(KISMockDispatchLineage)
                    .values(**values)
                    .on_conflict_do_nothing()
                )
            row = await self._lineage(session, envelope)
            if attempt["broker_order_id"] is not None:
                await self._append(
                    session,
                    row,
                    kind="ack",
                    evidence_key=_ACK_ENVELOPE_KEY,
                    odno=attempt["broker_order_id"],
                    payload={"envelope": payload},
                )
            await session.commit()

    async def _append(
        self,
        session: AsyncSession,
        row: KISMockDispatchLineage,
        *,
        kind: str,
        evidence_key: str,
        odno: str | None,
        payload: dict[str, Any],
    ) -> None:
        if kind not in KIS_MOCK_LANE_EVIDENCE_KINDS:
            raise KISMockEvidenceConflict("evidence_kind_rejected")
        if odno is not None and (
            type(odno) is not str or not re.fullmatch(r"[0-9]+", odno)
        ):
            raise KISMockEvidenceConflict("exact_odno_required")
        if kind not in {"unknown", "reject"} and odno is None:
            raise KISMockEvidenceConflict("exact_odno_required")
        if odno is not None:
            bound = await session.scalars(
                select(KISMockDispatchEvidence.broker_order_id).where(
                    KISMockDispatchEvidence.order_attempt_id == row.order_attempt_id,
                    KISMockDispatchEvidence.broker_order_id.is_not(None),
                )
            )
            if any(value != odno for value in bound):
                raise KISMockEvidenceConflict("attributed_odno_mismatch")
        key = _key(evidence_key)
        values = {
            "order_attempt_id": row.order_attempt_id,
            "kind": kind,
            "broker_evidence_key": key,
            "broker_order_id": odno,
            "payload": payload,
        }
        await session.execute(
            insert(KISMockDispatchEvidence)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["order_attempt_id", "kind", "broker_evidence_key"]
            )
        )
        existing = await session.scalar(
            select(KISMockDispatchEvidence).where(
                KISMockDispatchEvidence.order_attempt_id == row.order_attempt_id,
                KISMockDispatchEvidence.kind == kind,
                KISMockDispatchEvidence.broker_evidence_key == key,
            )
        )
        if (
            existing is None
            or existing.broker_order_id != odno
            or existing.payload != payload
        ):
            raise KISMockEvidenceConflict("immutable_evidence_conflict")

    async def persist_dispatch_evidence(self, evidence: DispatchEvidence, /) -> None:
        if type(evidence) is not DispatchEvidence:
            raise KISMockEvidenceConflict("dispatch_type_rejected")
        payload = _payload(evidence.envelope)
        attempt, plan = payload["order_attempt"], payload["execution_plan"]
        if (
            type(evidence.kind) is not DispatchEvidenceKind
            or type(evidence.certainty) is not MutationCertainty
            or type(evidence.claim_row_id) is not int
            or evidence.claim_row_id <= 0
            or any(
                type(v) is not bool
                for v in (
                    evidence.callback_failed,
                    evidence.ack_attachment_failed,
                    evidence.outer_cancellation_requested,
                )
            )
            or evidence.claim_account_scope != self._scope
            or evidence.idempotency_key != attempt["idempotency_key"]
            or evidence.order_attempt_id != attempt["order_attempt_id"]
            or evidence.execution_plan_id != plan["execution_plan_id"]
            or evidence.decision_intent_id
            != payload["decision_intent"]["decision_intent_id"]
            or evidence.cycle_id != attempt["cycle_id"]
            or evidence.attempt_seq != evidence.envelope.attempt_seq
            or evidence.broker_order_id != attempt["broker_order_id"]
        ):
            raise KISMockEvidenceConflict("dispatch_binding_mismatch")
        async with self._session_factory() as session:
            row = await self._lineage(session, evidence.envelope)
            claim = await session.get(OrderSendIntent, evidence.claim_row_id)
            if claim is None or (
                claim.account_scope,
                claim.idempotency_key,
                claim.side,
            ) != (
                self._scope,
                evidence.idempotency_key,
                payload["decision_intent"]["side"],
            ):
                raise KISMockEvidenceConflict("dispatch_claim_mismatch")
            acknowledged = (
                evidence.kind is DispatchEvidenceKind.ACKNOWLEDGED
                and evidence.certainty is MutationCertainty.DEFINITIVE
                and evidence.broker_order_id is not None
            )
            await self._append(
                session,
                row,
                kind="ack" if acknowledged else "unknown",
                evidence_key=_DISPATCH_EVIDENCE_KEY,
                odno=evidence.broker_order_id,
                payload={
                    "envelope": payload,
                    "dispatch_kind": evidence.kind.value,
                    "certainty": evidence.certainty.value,
                    "claim_row_id": evidence.claim_row_id,
                    "callback_failed": evidence.callback_failed,
                    "ack_attachment_failed": evidence.ack_attachment_failed,
                    "outer_cancellation_requested": evidence.outer_cancellation_requested,
                },
            )
            await session.commit()

    async def record_native_evidence(
        self, envelope: LineageEnvelope, evidence: NativeOrderEvidence
    ) -> None:
        if (
            type(evidence) is not NativeOrderEvidence
            or evidence.kind not in KIS_MOCK_LANE_EVIDENCE_KINDS
        ):
            raise KISMockEvidenceConflict("native_evidence_shape_rejected")
        if evidence.broker_evidence_key in (_DISPATCH_EVIDENCE_KEY, _ACK_ENVELOPE_KEY):
            raise KISMockEvidenceConflict("reserved_evidence_key")
        if (
            type(evidence.terminal) is not TerminalClaimEvidence
            or not evidence.terminal.exact_booleans
        ):
            raise KISMockEvidenceConflict("terminal_evidence_shape_rejected")
        if (
            evidence.kind != "terminal_reconciliation"
            and evidence.terminal != TerminalClaimEvidence()
        ):
            raise KISMockEvidenceConflict("terminal_reconciliation_required")
        if evidence.filled_quantity is not None and (
            type(evidence.filled_quantity) is not int or evidence.filled_quantity < 0
        ):
            raise KISMockEvidenceConflict("filled_quantity_invalid")
        async with self._session_factory() as session:
            row = await self._lineage(session, envelope)
            await self._verify_position_claim(session, row, envelope, evidence)
            odno = await self._attributed_odno(session, row.order_attempt_id)
            if (
                evidence.kind not in {"unknown", "reject"}
                or evidence.broker_order_id is not None
            ):
                if (
                    odno is None
                    or evidence.broker_order_id != odno
                    or evidence.readback_order_id != odno
                ):
                    raise KISMockEvidenceConflict("exact_attributed_readback_required")
            payload: dict[str, Any] = {
                "terminal": asdict(evidence.terminal),
                "readback_order_id": evidence.readback_order_id,
            }
            if evidence.filled_quantity is not None:
                payload["filled_quantity"] = evidence.filled_quantity
            if evidence.position_proof is not None:
                payload["position_proof"] = asdict(evidence.position_proof)
            await self._append(
                session,
                row,
                kind=evidence.kind,
                evidence_key=evidence.broker_evidence_key,
                odno=evidence.broker_order_id,
                payload=payload,
            )
            await session.commit()

    async def _verify_position_claim(
        self,
        session: AsyncSession,
        row: KISMockDispatchLineage,
        envelope: LineageEnvelope,
        evidence: NativeOrderEvidence,
    ) -> None:
        """No caller's boolean can assert position; the proof must recompute."""
        proof = evidence.position_proof
        if not evidence.terminal.account_position_reconciled:
            if proof is not None:
                raise KISMockEvidenceConflict("position_proof_not_claimed")
            return
        plan = envelope.execution_plan
        baseline = (
            await session.execute(
                select(_POSITION_BASELINE).where(
                    _POSITION_BASELINE.c.order_attempt_id == row.order_attempt_id
                )
            )
        ).first()
        if type(proof) is not PositionProof or baseline is None or plan is None:
            raise KISMockEvidenceConflict("position_proof_required")
        try:
            expected = expected_quantity(
                side=envelope.decision_intent.side,
                baseline=baseline.quantity,
                filled=proof.filled_quantity,
            )
        except PositionUnproven as exc:
            raise KISMockEvidenceConflict("position_proof_mismatch") from exc
        if (
            baseline.symbol != plan.normalized_symbol
            or proof.side != envelope.decision_intent.side
            or proof.baseline_quantity != baseline.quantity
            or proof.filled_quantity != evidence.filled_quantity
            or proof.observed_quantity != expected
        ):
            raise KISMockEvidenceConflict("position_proof_mismatch")

    async def record_position_baseline(
        self, envelope: LineageEnvelope, snapshot: BalanceSnapshot
    ) -> None:
        """Commit the pre-send holding once; the caller must do this before reserving."""
        plan = envelope.execution_plan
        if (
            type(snapshot) is not BalanceSnapshot
            or plan is None
            or snapshot.symbol != plan.normalized_symbol
            or type(snapshot.quantity) is not int
            or snapshot.quantity < 0
        ):
            raise KISMockEvidenceConflict("position_baseline_shape_rejected")
        async with self._session_factory() as session:
            row = await self._lineage(session, envelope)
            values = {
                "order_attempt_id": row.order_attempt_id,
                "symbol": snapshot.symbol,
                "quantity": snapshot.quantity,
                "read_digest": snapshot.read_digest,
            }
            await session.execute(
                insert(_POSITION_BASELINE).values(**values).on_conflict_do_nothing()
            )
            existing = (
                await session.execute(
                    select(_POSITION_BASELINE).where(
                        _POSITION_BASELINE.c.order_attempt_id == row.order_attempt_id
                    )
                )
            ).first()
            if existing is None or any(
                getattr(existing, key) != value for key, value in values.items()
            ):
                raise KISMockEvidenceConflict("position_baseline_conflict")
            await session.commit()

    async def load_position_baseline(self, lineage: KISMockRecoveryLineage) -> int:
        """Return the holding committed before this exact claim, or refuse."""
        envelope, plan = lineage.envelope, lineage.envelope.execution_plan
        attempt = envelope.order_attempt
        if plan is None or attempt is None:
            raise PositionUnproven("position_baseline_unavailable")
        async with self._session_factory() as session:
            baseline = (
                await session.execute(
                    select(_POSITION_BASELINE).where(
                        _POSITION_BASELINE.c.order_attempt_id
                        == attempt.order_attempt_id
                    )
                )
            ).first()
        if baseline is None or baseline.symbol != plan.normalized_symbol:
            raise PositionUnproven("position_baseline_unavailable")
        if (
            baseline.created_at > lineage.created_at
            or lineage.created_at - baseline.created_at > MAX_BASELINE_AGE
        ):
            raise PositionUnproven("position_baseline_stale")
        return int(baseline.quantity)

    async def attributed_filled_quantity(self, claim: RediscoveredKISMockClaim) -> int:
        """Return the one fill quantity committed for this exact attempt and ODNO."""
        if claim.order_attempt_id is None or claim.broker_order_id is None:
            raise PositionUnproven("attributed_fills_unavailable")
        async with self._session_factory() as session:
            events = await session.scalars(
                select(KISMockDispatchEvidence).where(
                    KISMockDispatchEvidence.order_attempt_id == claim.order_attempt_id,
                    KISMockDispatchEvidence.broker_order_id == claim.broker_order_id,
                    KISMockDispatchEvidence.kind.in_(_TERMINAL_FILL_KINDS),
                )
            )
            values = {
                event.payload["filled_quantity"]
                for event in events
                if type(event.payload) is dict and "filled_quantity" in event.payload
            }
        if len(values) != 1:
            raise PositionUnproven("attributed_fills_unavailable")
        quantity = values.pop()
        if type(quantity) is not int or quantity < 0:
            raise PositionUnproven("attributed_fills_unavailable")
        return quantity

    async def _attributed_odno(
        self, session: AsyncSession, attempt_id: str
    ) -> str | None:
        values = set(
            await session.scalars(
                select(KISMockDispatchEvidence.broker_order_id).where(
                    KISMockDispatchEvidence.order_attempt_id == attempt_id,
                    KISMockDispatchEvidence.kind == "ack",
                    KISMockDispatchEvidence.broker_order_id.is_not(None),
                )
            )
        )
        if len(values) > 1:
            raise KISMockEvidenceConflict("attributed_odno_ambiguous")
        return next(iter(values), None)

    async def rediscover_unreleased_claims(
        self,
    ) -> tuple[RediscoveredKISMockClaim, ...]:
        async with self._session_factory() as session:
            claims = await session.scalars(
                select(OrderSendIntent)
                .where(OrderSendIntent.account_scope == self._scope)
                .order_by(OrderSendIntent.id)
            )
            result = []
            for claim in claims:
                row = await session.scalar(
                    select(KISMockDispatchLineage).where(
                        KISMockDispatchLineage.claim_account_scope == self._scope,
                        KISMockDispatchLineage.idempotency_key == claim.idempotency_key,
                    )
                )
                attribution_error = None
                try:
                    odno = (
                        None
                        if row is None
                        else await self._attributed_odno(session, row.order_attempt_id)
                    )
                except KISMockEvidenceConflict:
                    odno = None
                    attribution_error = "attributed_odno_ambiguous"
                result.append(
                    RediscoveredKISMockClaim(
                        DurableClaim(
                            row_id=claim.id,
                            claim_account_scope=self._scope,
                            idempotency_key=claim.idempotency_key,
                            side=claim.side,
                        ),
                        None if row is None else row.order_attempt_id,
                        odno,
                        # An ACK identifies the order; it does not establish
                        # terminal reconciliation or release this account.
                        True,
                        attribution_error,
                    )
                )
            return tuple(result)

    async def load_recovery_lineage(
        self, claim: RediscoveredKISMockClaim
    ) -> KISMockRecoveryLineage:
        """Reconstruct the immutable lineage only for the exact surviving claim."""
        if (
            type(claim) is not RediscoveredKISMockClaim
            or claim.order_attempt_id is None
        ):
            raise KISMockEvidenceConflict("recovery_lineage_unavailable")
        async with self._session_factory() as session:
            row = await session.get(KISMockDispatchLineage, claim.order_attempt_id)
            current = await session.get(OrderSendIntent, claim.claim.row_id)
            if (
                row is None
                or current is None
                or row.physical_account_id != self._identity
                or row.claim_account_scope != self._scope
                or (current.account_scope, current.idempotency_key, current.side)
                != (
                    claim.claim.claim_account_scope,
                    claim.claim.idempotency_key,
                    claim.claim.side,
                )
                or row.idempotency_key != claim.claim.idempotency_key
                or claim.claim.claim_account_scope != self._scope
                or not isinstance(row.created_at, datetime)
                or not isinstance(current.created_at, datetime)
                or row.created_at.tzinfo is None
                or row.created_at.utcoffset() is None
                or current.created_at.tzinfo is None
                or current.created_at.utcoffset() is None
                or row.created_at > current.created_at
            ):
                raise KISMockEvidenceConflict("recovery_claim_binding_mismatch")
            envelope = LineageEnvelope.model_validate_json(
                json.dumps(row.initial_envelope, allow_nan=False)
            )
            if (
                _payload(envelope) != row.initial_envelope
                or envelope.order_attempt is None
                or envelope.order_attempt.order_attempt_id != claim.order_attempt_id
                or envelope.decision_intent.side != claim.claim.side
            ):
                raise KISMockEvidenceConflict("recovery_lineage_binding_mismatch")
            acknowledgements = await session.scalars(
                select(KISMockDispatchEvidence).where(
                    KISMockDispatchEvidence.order_attempt_id == claim.order_attempt_id,
                    KISMockDispatchEvidence.kind == "ack",
                    KISMockDispatchEvidence.broker_order_id == claim.broker_order_id,
                    KISMockDispatchEvidence.broker_evidence_key.in_(
                        (_key(_ACK_ENVELOPE_KEY), _key(_DISPATCH_EVIDENCE_KEY))
                    ),
                )
            )
            claim_day = current.created_at.astimezone(_KST).date()
            valid_ack_times = [
                event.created_at
                for event in acknowledgements
                if isinstance(event.created_at, datetime)
                and event.created_at.utcoffset() is not None
                and event.created_at >= current.created_at
                and event.created_at.astimezone(_KST).date() == claim_day
                and (
                    event.broker_evidence_key == _key(_ACK_ENVELOPE_KEY)
                    or _dispatch_matches_claim(event, current.id)
                )
            ]
            if not valid_ack_times:
                raise KISMockEvidenceConflict("recovery_dispatch_day_ambiguous")
            return KISMockRecoveryLineage(
                envelope=envelope,
                created_at=current.created_at,
                acknowledged_at=min(valid_ack_times),
            )

    async def has_unresolved_account_uncertainty(
        self, *, claim_account_scope: str
    ) -> bool:
        if claim_account_scope != self._scope:
            raise KISMockEvidenceConflict("claim_scope_mismatch")
        return any(
            claim.blocks_account for claim in await self.rediscover_unreleased_claims()
        )

    async def readback_by_exact_odno(
        self,
        claim: RediscoveredKISMockClaim,
        readback: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Only attributed ODNO reaches the operator-supplied broker reader."""
        current = [
            item for item in await self.rediscover_unreleased_claims() if item == claim
        ]
        if not current or claim.broker_order_id is None:
            raise KISMockEvidenceConflict("attributed_odno_required")
        result = await readback(claim.broker_order_id)
        if type(result) is not dict or result.get("odno") != claim.broker_order_id:
            raise KISMockEvidenceConflict("exact_attributed_readback_required")
        return result

    async def release_with_recorded_terminal_evidence(
        self,
        envelope: LineageEnvelope,
        *,
        broker_evidence_key: str,
        claim: DurableClaim,
        claims: DurableSendClaimAdapter,
    ) -> int:
        async with self._session_factory() as session:
            row = await self._lineage(session, envelope)
            event = await session.scalar(
                select(KISMockDispatchEvidence).where(
                    KISMockDispatchEvidence.order_attempt_id == row.order_attempt_id,
                    KISMockDispatchEvidence.kind == "terminal_reconciliation",
                    KISMockDispatchEvidence.broker_evidence_key
                    == _key(broker_evidence_key),
                )
            )
            dispatch = list(
                await session.scalars(
                    select(KISMockDispatchEvidence).where(
                        KISMockDispatchEvidence.order_attempt_id
                        == row.order_attempt_id,
                        KISMockDispatchEvidence.broker_evidence_key
                        == _key(_DISPATCH_EVIDENCE_KEY),
                    )
                )
            )
            if (
                event is None
                or not dispatch
                or claim.claim_account_scope != self._scope
                or claim.idempotency_key != row.idempotency_key
                or claim.side != envelope.decision_intent.side
                or not all(
                    _dispatch_matches_claim(item, claim.row_id) for item in dispatch
                )
            ):
                raise KISMockEvidenceConflict("terminal_claim_binding_mismatch")
            terminal = TerminalClaimEvidence(**event.payload["terminal"])
        return await claims.release_with_terminal_evidence(claim, terminal)

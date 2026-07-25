"""Durable terminal fence and bounded cleanup for ROB-849 cohorts."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortTerminalFence,
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_validation import PaperValidationStateTransition
from app.services.alpaca_paper_ledger_service import (
    _redact_sensitive_keys,
    _redact_sensitive_text,
)
from app.services.brokers.paper.contracts import ExperimentProvenanceVerifier
from app.services.paper_cohort.contracts import (
    PaperCohortError,
    PaperCohortKillRequest,
    PaperCohortKillResult,
    PaperCohortLinkCleanupResult,
    RunMode,
)
from app.services.paper_cohort.order_control import PaperCohortOrderControl
from app.services.paper_cohort.runner import CohortRunInvocation
from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    ActorRoleProvider,
)
from app.services.paper_validation.locking import lock_validation_streams
from app.services.research_canonical_hash import canonical_sha256

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]
CleanupPass = Callable[[str], Awaitable[tuple[PaperCohortLinkCleanupResult, ...]]]
RecoveryPass = Callable[[str], Awaitable[None]]
VerifierFactory = Callable[[AsyncSession], ExperimentProvenanceVerifier]


class RecoveryRunner(Protocol):
    async def recover(self, invocation: CohortRunInvocation) -> object: ...


RecoveryRunnerFactory = Callable[[AsyncSession], RecoveryRunner]


class PaperCohortRecoveryPass:
    """Resolve/link prepared native orders without issuing a fresh POST."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        runner_factory: RecoveryRunnerFactory,
    ) -> None:
        self._session_factory = session_factory
        self._runner_factory = runner_factory

    async def run(self, cohort_id: str) -> None:
        async with self._session_factory() as discovery:
            rows = (
                await discovery.execute(
                    select(PaperCohortRunClaim, PaperCohortDecision.mode)
                    .distinct()
                    .join(
                        PaperCohortDecision,
                        (PaperCohortDecision.cohort_id == PaperCohortRunClaim.cohort_id)
                        & (PaperCohortDecision.run_id == PaperCohortRunClaim.run_id)
                        & (
                            PaperCohortDecision.round_decision_id
                            == PaperCohortRunClaim.round_decision_id
                        ),
                    )
                    .where(
                        PaperCohortRunClaim.cohort_id == cohort_id,
                        PaperCohortRunClaim.completed_at.is_(None),
                    )
                    .order_by(PaperCohortRunClaim.created_at)
                )
            ).all()

        failed = False
        for claim, mode in rows:
            invocation = CohortRunInvocation(
                cohort_id=claim.cohort_id,
                run_id=claim.run_id,
                round_decision_id=claim.round_decision_id,
                mode=RunMode(mode),
            )
            async with self._session_factory() as session:
                try:
                    # PaperCohortRunner.recover is the recovery-only boundary:
                    # it verifies persisted provenance and resolve/links native
                    # orders, but never constructs a submit application.
                    await self._runner_factory(session).recover(invocation)
                except Exception as exc:  # noqa: BLE001 - finish other claims
                    failed = True
                    await session.rollback()
                    logger.warning(
                        "paper cohort recovery failed (%s)", type(exc).__name__
                    )
        if failed:
            raise PaperCohortError("recovery_incomplete")


class PaperCohortCleanupPass:
    """Clean only native links already and immutably owned by one cohort."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        verifier_factory: VerifierFactory,
    ) -> None:
        self._session_factory = session_factory
        self._verifier_factory = verifier_factory

    async def run(self, cohort_id: str) -> tuple[PaperCohortLinkCleanupResult, ...]:
        async with self._session_factory() as session:
            links = list(
                (
                    await session.scalars(
                        select(PaperRunOrderLink)
                        .where(PaperRunOrderLink.cohort_id == cohort_id)
                        .order_by(PaperRunOrderLink.id)
                    )
                ).all()
            )
            control = PaperCohortOrderControl(
                session,
                verifier=self._verifier_factory(session),
            )
            outcomes: list[PaperCohortLinkCleanupResult] = []
            for link in links:
                try:
                    outcomes.append(await control.cleanup(cohort_id, link.id))
                except PaperCohortError:
                    outcomes.append(
                        PaperCohortLinkCleanupResult(
                            link_id=link.id,
                            venue=link.venue,
                            status="manual_required",
                            action="none",
                            reason_code="owned_cleanup_evidence_invalid",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - per-link fail-closed edge
                    # Do not log provider messages: they can echo credentials.
                    logger.warning(
                        "paper cohort cleanup failed (%s)", type(exc).__name__
                    )
                    outcomes.append(
                        PaperCohortLinkCleanupResult(
                            link_id=link.id,
                            venue=link.venue,
                            status="pending",
                            action="none",
                            reason_code="cleanup_failed",
                        )
                    )
            return tuple(outcomes)


class PaperCohortKillSwitch:
    """Commit an immutable fence before attempting any native side effect."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        actor_role_provider: ActorRoleProvider,
        recovery_pass: RecoveryPass,
        cleanup_pass: CleanupPass,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._actor_roles = actor_role_provider
        self._recovery_pass = recovery_pass
        self._cleanup_pass = cleanup_pass
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self, caller_id: str, request: PaperCohortKillRequest
    ) -> PaperCohortKillResult:
        try:
            actor = await self._actor_roles.resolve(caller_id)
        except LookupError as exc:
            raise PaperCohortError("actor_identity_unavailable") from exc
        if actor.role not in {ActorRole.OPERATOR, ActorRole.SYSTEM}:
            raise PaperCohortError("kill_switch_forbidden")

        # This transaction ends (and therefore commits) before cleanup starts.
        # A provider timeout or process crash can only leave cleanup pending; it
        # cannot reopen a fenced cohort.
        async with self._session_factory() as session:
            async with session.begin():
                fence, replayed = await self._persist_fence(
                    session, actor=actor, request=request
                )

        recovery_pending = False
        try:
            await self._recovery_pass(request.cohort_id)
        except Exception as exc:  # noqa: BLE001 - fence remains authoritative
            recovery_pending = True
            logger.warning("paper cohort recovery pass failed (%s)", type(exc).__name__)

        try:
            cleanup_results = await self._cleanup_pass(request.cohort_id)
            cleanup_status = self._cleanup_status(cleanup_results)
        except Exception as exc:  # noqa: BLE001 - fence must survive cleanup faults
            logger.warning("paper cohort cleanup pass failed (%s)", type(exc).__name__)
            cleanup_results = ()
            cleanup_status = "pending"
        if recovery_pending:
            cleanup_status = "pending"

        return PaperCohortKillResult(
            status="already_fenced" if replayed else "fenced",
            fence_id=fence.fence_id,
            cohort_id=fence.cohort_id,
            fenced_at=fence.fenced_at,
            replayed=replayed,
            cleanup_status=cleanup_status,
            cleanup_results=cleanup_results,
        )

    async def _persist_fence(
        self,
        session: AsyncSession,
        *,
        actor: ActorIdentity,
        request: PaperCohortKillRequest,
    ) -> tuple[PaperCohortTerminalFence, bool]:
        cohort = await self._cohort(session, request.cohort_id)
        if cohort is None:
            raise PaperCohortError("cohort_not_found")
        assignments = await self._assignments(session, request.cohort_id)
        if not assignments or len(assignments) != cohort.assignment_count:
            raise PaperCohortError("cohort_assignment_mismatch")

        # ROB-848 validation locks have a single total order. Cohort activation
        # and terminal fencing both take those sorted locks before the cohort
        # lock, preventing activation/fencing deadlocks and TOCTOU authorization.
        await lock_validation_streams(
            session, (assignment.validation_id for assignment in assignments)
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"paper-cohort:{request.cohort_id}"},
        )

        cohort = await self._cohort(session, request.cohort_id)
        locked_assignments = await self._assignments(session, request.cohort_id)
        if (
            cohort is None
            or not locked_assignments
            or len(locked_assignments) != cohort.assignment_count
            or tuple(item.assignment_id for item in locked_assignments)
            != tuple(item.assignment_id for item in assignments)
        ):
            raise PaperCohortError("cohort_assignment_mismatch")

        safe_reason_code = _redact_sensitive_text(request.reason_code)
        safe_reason_text = _redact_sensitive_text(request.reason_text)
        if not safe_reason_code or not safe_reason_text:
            raise PaperCohortError("kill_reason_invalid")
        request_hash = canonical_sha256(
            {
                "schema_id": "paper_cohort_terminal_fence_request.v1",
                "cohort_id": request.cohort_id,
                "cohort_hash": cohort.cohort_hash,
                "idempotency_key": request.idempotency_key,
                "reason_code": safe_reason_code,
                "reason_text": safe_reason_text,
                "actor_id": actor.actor_id,
                "actor_role": actor.role.value,
            }
        )
        existing = await session.scalar(
            select(PaperCohortTerminalFence).where(
                PaperCohortTerminalFence.cohort_id == request.cohort_id
            )
        )
        if existing is not None:
            if (
                existing.idempotency_key == request.idempotency_key
                and existing.request_hash == request_hash
            ):
                return existing, True
            raise PaperCohortError("kill_switch_idempotency_conflict")

        evidence = await self._validation_evidence(
            session,
            cohort=cohort,
            assignments=locked_assignments,
        )
        fenced_at = self._clock()
        if fenced_at.tzinfo is None or fenced_at.tzinfo.utcoffset(fenced_at) is None:
            raise PaperCohortError("kill_switch_clock_invalid")
        row = PaperCohortTerminalFence(
            fence_id=f"pcf-{request_hash}",
            cohort_id=request.cohort_id,
            cohort_hash=cohort.cohort_hash,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            reason_code=safe_reason_code,
            reason_text=safe_reason_text,
            validation_evidence=_redact_sensitive_keys(evidence),
            fenced_at=fenced_at,
        )
        session.add(row)
        await session.flush()
        return row, False

    @staticmethod
    async def _cohort(
        session: AsyncSession, cohort_id: str
    ) -> PaperValidationCohort | None:
        return await session.scalar(
            select(PaperValidationCohort).where(
                PaperValidationCohort.cohort_id == cohort_id
            )
        )

    @staticmethod
    async def _assignments(
        session: AsyncSession, cohort_id: str
    ) -> list[PaperValidationCohortAssignment]:
        return list(
            (
                await session.scalars(
                    select(PaperValidationCohortAssignment)
                    .where(PaperValidationCohortAssignment.cohort_id == cohort_id)
                    .order_by(PaperValidationCohortAssignment.ordinal)
                )
            ).all()
        )

    @staticmethod
    async def _validation_evidence(
        session: AsyncSession,
        *,
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
    ) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for assignment in assignments:
            latest = await session.scalar(
                select(PaperValidationStateTransition)
                .where(
                    PaperValidationStateTransition.validation_id
                    == assignment.validation_id
                )
                .order_by(PaperValidationStateTransition.sequence.desc())
                .limit(1)
            )
            if latest is None or not all(
                (
                    latest.validation_version == assignment.validation_version,
                    latest.experiment_id == assignment.experiment_id,
                    latest.strategy_version_id == assignment.strategy_version_id,
                    latest.cohort_id == cohort.cohort_id,
                    latest.experiment_hash == assignment.experiment_hash,
                    latest.cohort_hash == cohort.cohort_hash,
                    latest.strategy_hash == assignment.strategy_hash,
                    latest.config_hash == assignment.config_hash,
                    latest.policy_hash == assignment.policy_hash,
                    latest.input_hash == assignment.input_hash,
                )
            ):
                raise PaperCohortError("validation_identity_mismatch")
            items.append(
                {
                    "validation_id": latest.validation_id,
                    "validation_version": latest.validation_version,
                    "sequence": latest.sequence,
                    "state": latest.new_state,
                    "transition_request_hash": latest.request_hash,
                    "experiment_id": latest.experiment_id,
                    "strategy_version_id": latest.strategy_version_id,
                    "experiment_hash": latest.experiment_hash,
                    "cohort_hash": latest.cohort_hash,
                    "strategy_hash": latest.strategy_hash,
                    "config_hash": latest.config_hash,
                    "policy_hash": latest.policy_hash,
                    "input_hash": latest.input_hash,
                }
            )
        return {
            "schema_id": "paper_cohort_terminal_fence_evidence.v1",
            "validations": items,
        }

    @staticmethod
    def _cleanup_status(
        results: tuple[PaperCohortLinkCleanupResult, ...],
    ) -> str:
        if any(item.status == "manual_required" for item in results):
            return "manual_required"
        if any(item.status == "pending" for item in results):
            return "pending"
        return "complete"


__all__ = [
    "PaperCohortCleanupPass",
    "PaperCohortKillSwitch",
    "PaperCohortRecoveryPass",
]

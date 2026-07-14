"""Default-off production orchestration for ROB-849 cohorts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.paper_cohort import (
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortTerminalFence,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_validation import PaperValidationStateTransition
from app.services.brokers.binance.rest_client import BinancePublicRestClient
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotCapture
from app.services.paper_cohort.provenance import (
    CohortFrozenInputHashProvider,
    CohortPolicyHashProvider,
    PaperCohortProvenanceVerifier,
)
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_cohort.venue_quotes import (
    AlpacaCryptoQuoteClient,
    ProductionVenueQuoteProvider,
)
from app.services.paper_validation.contracts import ActorIdentity, ActorRole
from app.services.paper_validation.service import PaperValidationService
from app.services.research_canonical_hash import canonical_sha256


class _ConfiguredActorRoleProvider:
    async def resolve(self, caller_id: str) -> ActorIdentity:
        try:
            role = ActorRole(settings.PAPER_VALIDATION_ACTOR_ROLES[caller_id])
        except (KeyError, ValueError) as exc:
            raise PaperCohortError("actor_identity_unavailable") from exc
        return ActorIdentity(actor_id=caller_id, role=role)


async def _runtime_mode(session: Any, cohort_id: str) -> RunMode | None:
    assignments = (
        await session.scalars(
            select(PaperValidationCohortAssignment).where(
                PaperValidationCohortAssignment.cohort_id == cohort_id
            )
        )
    ).all()
    states: list[str] = []
    for assignment in assignments:
        latest = await session.scalar(
            select(PaperValidationStateTransition.new_state)
            .where(
                PaperValidationStateTransition.validation_id == assignment.validation_id
            )
            .order_by(PaperValidationStateTransition.sequence.desc())
            .limit(1)
        )
        if latest is None:
            return None
        states.append(latest)
    if states and all(state == "paper_active" for state in states):
        return RunMode.PAPER_ACTIVE
    if states and all(state == "shadow_soak" for state in states):
        return RunMode.SHADOW
    return None


def _invocation(
    cohort_id: str,
    cohort_hash: str,
    mode: RunMode,
    now: datetime,
) -> CohortRunInvocation:
    identity_input: dict[str, object] = {
        "cohort_id": cohort_id,
        "cohort_hash": cohort_hash,
        "mode": mode,
    }
    prefix = "observation"
    if mode is RunMode.SHADOW:
        identity_input["minute"] = now.astimezone(UTC).replace(second=0, microsecond=0)
    else:
        prefix = "one-shot"
    identity = canonical_sha256(identity_input)
    return CohortRunInvocation(
        cohort_id=cohort_id,
        run_id=f"scheduled-{identity[:40]}",
        round_decision_id=f"{prefix}-{identity[:40]}",
        mode=mode,
    )


async def _recoverable_invocations(session: Any) -> list[CohortRunInvocation]:
    rows = (
        await session.execute(
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
                PaperCohortRunClaim.claim_status.in_(
                    ("in_progress", "reconciliation_required")
                ),
            )
            .order_by(PaperCohortRunClaim.created_at)
        )
    ).all()
    return [
        CohortRunInvocation(
            cohort_id=row[0].cohort_id,
            run_id=row[0].run_id,
            round_decision_id=row[0].round_decision_id,
            mode=RunMode(row[1]),
        )
        for row in rows
    ]


class _RecoveryOnlyBoundary:
    async def capture(self, request):  # noqa: ANN001, ANN202
        del request
        raise AssertionError("recovery must not capture a new snapshot")

    async def get_quote(self, venue, symbol):  # noqa: ANN001, ANN202
        del venue, symbol
        raise AssertionError("recovery must not fetch a venue quote")


async def run_active_paper_cohorts(
    *,
    session_factory: Callable[[], Any] = AsyncSessionLocal,
    client_factory: Callable[[], BinancePublicRestClient] = BinancePublicRestClient,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    runtime_clock = clock or (lambda: datetime.now(UTC))
    now = runtime_clock()
    actor_id = settings.PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID.strip()

    async with session_factory() as discovery:
        recoverable = await _recoverable_invocations(discovery)
        await discovery.rollback()
    outcomes: list[dict[str, object]] = []
    for invocation in recoverable:
        async with session_factory() as session:
            try:
                cohort = await session.scalar(
                    select(PaperValidationCohort).where(
                        PaperValidationCohort.cohort_id == invocation.cohort_id
                    )
                )
                terminal_fence = await session.scalar(
                    select(PaperCohortTerminalFence.id).where(
                        PaperCohortTerminalFence.cohort_id == invocation.cohort_id
                    )
                )
                current_mode = await _runtime_mode(session, invocation.cohort_id)
                fresh_resume_allowed = bool(
                    settings.PAPER_COHORT_ENABLED
                    and actor_id
                    and cohort is not None
                    and terminal_fence is None
                    and (cohort.stop_at is None or cohort.stop_at > now)
                    and current_mode is invocation.mode
                    and (
                        invocation.mode is RunMode.SHADOW
                        or settings.PAPER_EXECUTION_ENABLED
                    )
                )
                validation = PaperValidationService(
                    session,
                    actor_role_provider=_ConfiguredActorRoleProvider(),
                    frozen_input_provider=CohortFrozenInputHashProvider(session),
                    policy_provider=CohortPolicyHashProvider(session),
                )
                verifier = PaperCohortProvenanceVerifier(
                    session,
                    validation_service=validation,
                    caller_id=actor_id or "paper-cohort-recovery",
                    clock=runtime_clock,
                )
                unused = _RecoveryOnlyBoundary()
                runner = PaperCohortRunner(
                    session,
                    capture=unused,
                    quote_provider=unused,
                    verifier=verifier,
                    clock=runtime_clock,
                )
                result = (
                    await runner.run(invocation)
                    if fresh_resume_allowed
                    else await runner.recover(invocation)
                )
                outcomes.append(
                    {
                        "cohort_id": invocation.cohort_id,
                        "status": ("resumed" if fresh_resume_allowed else "recovered"),
                        **result.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - recovery fail-close boundary
                await session.rollback()
                outcomes.append(
                    {
                        "cohort_id": invocation.cohort_id,
                        "status": "failed",
                        "reason": getattr(exc, "reason_code", type(exc).__name__),
                    }
                )

    if not settings.PAPER_COHORT_ENABLED:
        return {"status": "disabled", "cohorts": outcomes}
    if not actor_id:
        return {
            "status": "blocked",
            "reason": "actor_identity_unavailable",
            "cohorts": outcomes,
        }

    async with session_factory() as discovery:
        cohorts = list(
            (
                await discovery.scalars(
                    select(PaperValidationCohort).where(
                        PaperValidationCohort.activated_at <= now,
                        (
                            PaperValidationCohort.stop_at.is_(None)
                            | (PaperValidationCohort.stop_at > now)
                        ),
                    )
                )
            ).all()
        )

    for cohort in cohorts:
        cohort_id = cohort.cohort_id
        async with session_factory() as session:
            mode = await _runtime_mode(session, cohort_id)
            if mode is None:
                outcomes.append({"cohort_id": cohort_id, "status": "state_blocked"})
                continue
            if mode is RunMode.PAPER_ACTIVE and not settings.PAPER_EXECUTION_ENABLED:
                outcomes.append(
                    {
                        "cohort_id": cohort_id,
                        "status": "state_blocked",
                        "reason": "paper_execution_disabled",
                    }
                )
                continue
            client = client_factory()
            alpaca_quotes: AlpacaCryptoQuoteClient | None = None
            try:
                alpaca_quotes = AlpacaCryptoQuoteClient()
                validation = PaperValidationService(
                    session,
                    actor_role_provider=_ConfiguredActorRoleProvider(),
                    frozen_input_provider=CohortFrozenInputHashProvider(session),
                    policy_provider=CohortPolicyHashProvider(session),
                )
                verifier = PaperCohortProvenanceVerifier(
                    session,
                    validation_service=validation,
                    caller_id=actor_id,
                    clock=runtime_clock,
                )
                result = await PaperCohortRunner(
                    session,
                    capture=CanonicalSnapshotCapture(client),
                    quote_provider=ProductionVenueQuoteProvider(client, alpaca_quotes),
                    verifier=verifier,
                    clock=runtime_clock,
                ).run(_invocation(cohort_id, cohort.cohort_hash, mode, now))
                await session.commit()
                outcomes.append(
                    {
                        "cohort_id": cohort_id,
                        "status": "completed",
                        **result.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - per-cohort fail-close boundary
                await session.rollback()
                outcomes.append(
                    {
                        "cohort_id": cohort_id,
                        "status": "failed",
                        "reason": getattr(exc, "reason_code", type(exc).__name__),
                    }
                )
            finally:
                if alpaca_quotes is not None:
                    await alpaca_quotes.aclose()
                await client.aclose()
    return {"status": "completed", "cohorts": outcomes}


__all__ = ["run_active_paper_cohorts"]

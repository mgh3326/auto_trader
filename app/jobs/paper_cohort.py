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


def _invocation(cohort_id: str, mode: RunMode, now: datetime) -> CohortRunInvocation:
    bucket = now.astimezone(UTC).replace(second=0, microsecond=0)
    identity = canonical_sha256(
        {"cohort_id": cohort_id, "minute": bucket, "mode": mode}
    )
    return CohortRunInvocation(
        cohort_id=cohort_id,
        run_id=f"scheduled-{identity[:40]}",
        round_decision_id=f"minute-{identity[:40]}",
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
                PaperCohortRunClaim.completed_at.is_(None),
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
    now = (clock or (lambda: datetime.now(UTC)))()
    actor_id = settings.PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID.strip()

    async with session_factory() as discovery:
        recoverable = await _recoverable_invocations(discovery)
        await discovery.rollback()
    outcomes: list[dict[str, object]] = []
    for invocation in recoverable:
        async with session_factory() as session:
            try:
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
                    clock=lambda: now,
                )
                unused = _RecoveryOnlyBoundary()
                result = await PaperCohortRunner(
                    session,
                    capture=unused,
                    quote_provider=unused,
                    verifier=verifier,
                    clock=lambda: now,
                ).recover(invocation)
                outcomes.append(
                    {
                        "cohort_id": invocation.cohort_id,
                        "status": "recovered",
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
        cohort_ids = list(
            (
                await discovery.scalars(
                    select(PaperValidationCohort.cohort_id).where(
                        PaperValidationCohort.activated_at <= now,
                        (
                            PaperValidationCohort.stop_at.is_(None)
                            | (PaperValidationCohort.stop_at > now)
                        ),
                    )
                )
            ).all()
        )

    for cohort_id in cohort_ids:
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
                    clock=lambda: now,
                )
                result = await PaperCohortRunner(
                    session,
                    capture=CanonicalSnapshotCapture(client),
                    quote_provider=ProductionVenueQuoteProvider(client, alpaca_quotes),
                    verifier=verifier,
                    clock=lambda: now,
                ).run(_invocation(cohort_id, mode, now))
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

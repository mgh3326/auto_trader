"""Transactional append-only service for ROB-848 paper validation."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_validation import (
    PaperValidationPostmortemReview,
    PaperValidationStateTransition,
    StrategyHypothesisDraft,
)
from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    ActorRoleProvider,
    FrozenInputHashProvider,
    FrozenInputStamp,
    HypothesisDraftInput,
    PaperOrderAuthorization,
    PolicyHashProvider,
    PolicyStamp,
    PostmortemReviewInput,
    PromotionConfirmationInput,
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
)
from app.services.paper_validation.state_machine import (
    decide_transition,
    is_order_authorizable,
)
from app.services.research_canonical_hash import canonical_sha256

_MUTATION_ROLES = frozenset({ActorRole.OPERATOR, ActorRole.SYSTEM})


class PaperValidationError(Exception):
    """Stable fail-closed paper-validation error."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class PaperValidationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_role_provider: ActorRoleProvider | None,
        frozen_input_provider: FrozenInputHashProvider | None,
        policy_provider: PolicyHashProvider | None,
    ) -> None:
        self._session = session
        self._actor_role_provider = actor_role_provider
        self._frozen_input_provider = frozen_input_provider
        self._policy_provider = policy_provider

    async def _resolve_actor(self, caller_id: str) -> ActorIdentity:
        if not caller_id.strip() or self._actor_role_provider is None:
            raise PaperValidationError("actor_identity_unavailable")
        try:
            return await self._actor_role_provider.resolve(caller_id)
        except Exception as exc:
            raise PaperValidationError("actor_identity_unavailable") from exc

    @staticmethod
    def _require_role(actor: ActorIdentity, allowed: Iterable[ActorRole]) -> None:
        if actor.role not in allowed:
            raise PaperValidationError("forbidden")

    async def _resolve_evidence(
        self, identity: ValidationIdentity
    ) -> tuple[FrozenInputStamp, PolicyStamp]:
        if self._frozen_input_provider is None or self._policy_provider is None:
            raise PaperValidationError("evidence_stamp_unavailable")
        try:
            frozen = FrozenInputStamp.model_validate(
                await self._frozen_input_provider.get_stamp(identity),
                from_attributes=True,
            )
            policy = PolicyStamp.model_validate(
                await self._policy_provider.get_stamp(identity),
                from_attributes=True,
            )
        except Exception as exc:
            raise PaperValidationError("evidence_stamp_unavailable") from exc
        if (
            frozen.content_hash != identity.input_hash
            or policy.content_hash != identity.policy_hash
        ):
            raise PaperValidationError("evidence_hash_mismatch")
        return frozen, policy

    async def _lock_validation(self, validation_id: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": validation_id},
        )

    async def _latest(
        self, validation_id: str
    ) -> PaperValidationStateTransition | None:
        return await self._session.scalar(
            select(PaperValidationStateTransition)
            .where(PaperValidationStateTransition.validation_id == validation_id)
            .order_by(PaperValidationStateTransition.sequence.desc())
            .limit(1)
        )

    async def _by_idempotency(
        self, validation_id: str, idempotency_key: str
    ) -> PaperValidationStateTransition | None:
        return await self._session.scalar(
            select(PaperValidationStateTransition).where(
                PaperValidationStateTransition.validation_id == validation_id,
                PaperValidationStateTransition.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _identity_matches(
        event: PaperValidationStateTransition, identity: ValidationIdentity
    ) -> bool:
        return all(
            (
                event.validation_version == identity.validation_version,
                event.experiment_id == identity.experiment_id,
                event.strategy_version_id == identity.strategy_version_id,
                event.cohort_id == identity.cohort_id,
                event.experiment_hash == identity.experiment_hash,
                event.cohort_hash == identity.cohort_hash,
                event.strategy_hash == identity.strategy_hash,
                event.config_hash == identity.config_hash,
                event.policy_hash == identity.policy_hash,
                event.input_hash == identity.input_hash,
            )
        )

    @staticmethod
    def _identity_from_event(
        event: PaperValidationStateTransition,
    ) -> ValidationIdentity:
        return ValidationIdentity(
            validation_id=event.validation_id,
            validation_version=event.validation_version,
            experiment_id=event.experiment_id,
            strategy_version_id=event.strategy_version_id,
            cohort_id=event.cohort_id,
            experiment_hash=event.experiment_hash,
            cohort_hash=event.cohort_hash,
            strategy_hash=event.strategy_hash,
            config_hash=event.config_hash,
            policy_hash=event.policy_hash,
            input_hash=event.input_hash,
        )

    @staticmethod
    def _request_hash(
        request: TransitionRequest,
        actor: ActorIdentity,
    ) -> str:
        return canonical_sha256(
            {
                "request": request.model_dump(mode="python"),
                "actor": actor.model_dump(mode="python"),
            }
        )

    async def transition(
        self,
        caller_id: str,
        request: TransitionRequest,
    ) -> PaperValidationStateTransition:
        return await self._append_transition(
            caller_id, request, promotion_confirmed=False
        )

    async def _append_transition(
        self,
        caller_id: str,
        request: TransitionRequest,
        *,
        promotion_confirmed: bool,
    ) -> PaperValidationStateTransition:
        actor = await self._resolve_actor(caller_id)
        self._require_role(actor, _MUTATION_ROLES)
        if request.target_state is ValidationState.PROMOTED and not promotion_confirmed:
            raise PaperValidationError("promotion_confirmation_required")
        request_hash = self._request_hash(request, actor)
        replay = await self._by_idempotency(
            request.identity.validation_id, request.idempotency_key
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")

        frozen, policy = await self._resolve_evidence(request.identity)
        promotion_evidence = frozen.promotion_eligibility
        trusted_gate_evidence: tuple[str, ...] = ()
        if request.target_state is ValidationState.PROMOTION_ELIGIBLE:
            if promotion_evidence is None:
                raise PaperValidationError("evidence_stamp_unavailable")
            if not promotion_evidence.deterministic_gate_passed:
                raise PaperValidationError("promotion_gate_blocked")
            if promotion_evidence.resolved_negative_class_count < 30:
                raise PaperValidationError("calibration_gate_blocked")
            trusted_gate_evidence = promotion_evidence.evidence_ids

        await self._lock_validation(request.identity.validation_id)
        replay = await self._by_idempotency(
            request.identity.validation_id, request.idempotency_key
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")

        latest = await self._latest(request.identity.validation_id)
        if latest is not None and not self._identity_matches(latest, request.identity):
            raise PaperValidationError("experiment_identity_mismatch")

        actual_prior = ValidationState(latest.new_state) if latest is not None else None
        if actual_prior is not request.expected_prior_state:
            raise PaperValidationError("concurrent_transition_conflict")
        decision = decide_transition(actual_prior, request.target_state)
        if not decision.allowed:
            raise PaperValidationError(decision.reason_code or "invalid_transition")

        identity = request.identity
        event = PaperValidationStateTransition(
            validation_id=identity.validation_id,
            validation_version=identity.validation_version,
            experiment_id=identity.experiment_id,
            strategy_version_id=identity.strategy_version_id,
            cohort_id=identity.cohort_id,
            sequence=1 if latest is None else latest.sequence + 1,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            prior_state=actual_prior.value if actual_prior is not None else None,
            new_state=request.target_state.value,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            reason_code=request.reason_code,
            reason_text=request.reason_text,
            experiment_hash=identity.experiment_hash,
            cohort_hash=identity.cohort_hash,
            strategy_hash=identity.strategy_hash,
            config_hash=identity.config_hash,
            policy_hash=identity.policy_hash,
            input_hash=identity.input_hash,
            input_bundle_id=frozen.bundle_id,
            policy_version=policy.version,
            evidence_ids=list(
                dict.fromkeys(
                    (
                        *request.evidence_ids,
                        *trusted_gate_evidence,
                    )
                )
            ),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def _trusted_current(
        self, validation_id: str
    ) -> PaperValidationStateTransition:
        latest = await self._latest(validation_id)
        if latest is None:
            raise PaperValidationError("validation_not_found")
        return latest

    async def append_hypothesis(
        self, caller_id: str, request: HypothesisDraftInput
    ) -> StrategyHypothesisDraft:
        actor = await self._resolve_actor(caller_id)
        self._require_role(actor, (ActorRole.RESEARCHER,))
        request_hash = canonical_sha256(
            {
                "request": request.model_dump(mode="python"),
                "actor": actor.model_dump(mode="python"),
            }
        )
        replay = await self._session.scalar(
            select(StrategyHypothesisDraft).where(
                StrategyHypothesisDraft.validation_id == request.validation_id,
                StrategyHypothesisDraft.idempotency_key == request.idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")
        observed = await self._trusted_current(request.validation_id)
        identity = self._identity_from_event(observed)
        await self._resolve_evidence(identity)

        await self._lock_validation(request.validation_id)
        replay = await self._session.scalar(
            select(StrategyHypothesisDraft).where(
                StrategyHypothesisDraft.validation_id == request.validation_id,
                StrategyHypothesisDraft.idempotency_key == request.idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")
        latest = await self._trusted_current(request.validation_id)
        if not self._identity_matches(latest, identity):
            raise PaperValidationError("experiment_identity_mismatch")
        row = StrategyHypothesisDraft(
            validation_id=identity.validation_id,
            validation_version=identity.validation_version,
            experiment_id=identity.experiment_id,
            strategy_version_id=identity.strategy_version_id,
            cohort_id=identity.cohort_id,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            author_id=actor.actor_id,
            author_role=actor.role.value,
            mechanism=request.mechanism,
            universe=list(request.universe),
            horizon=request.horizon,
            entry_criteria=list(request.entry_criteria),
            exit_criteria=list(request.exit_criteria),
            invalidation_criteria=list(request.invalidation_criteria),
            data_requirements=list(request.data_requirements),
            expected_cost_hurdle=request.expected_cost_hurdle,
            turnover_bound=request.turnover_bound,
            risk_bound=request.risk_bound,
            cited_evidence=list(request.cited_evidence),
            experiment_hash=identity.experiment_hash,
            cohort_hash=identity.cohort_hash,
            strategy_hash=identity.strategy_hash,
            config_hash=identity.config_hash,
            policy_hash=identity.policy_hash,
            input_hash=identity.input_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def append_postmortem_review(
        self, caller_id: str, request: PostmortemReviewInput
    ) -> PaperValidationPostmortemReview:
        actor = await self._resolve_actor(caller_id)
        self._require_role(actor, (ActorRole.REVIEWER,))
        request_hash = canonical_sha256(
            {
                "request": request.model_dump(mode="python"),
                "actor": actor.model_dump(mode="python"),
            }
        )
        replay = await self._session.scalar(
            select(PaperValidationPostmortemReview).where(
                PaperValidationPostmortemReview.validation_id == request.validation_id,
                PaperValidationPostmortemReview.idempotency_key
                == request.idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")
        observed = await self._trusted_current(request.validation_id)
        identity = self._identity_from_event(observed)
        await self._resolve_evidence(identity)

        await self._lock_validation(request.validation_id)
        replay = await self._session.scalar(
            select(PaperValidationPostmortemReview).where(
                PaperValidationPostmortemReview.validation_id == request.validation_id,
                PaperValidationPostmortemReview.idempotency_key
                == request.idempotency_key,
            )
        )
        if replay is not None:
            if replay.request_hash == request_hash:
                return replay
            raise PaperValidationError("idempotency_conflict")
        latest = await self._trusted_current(request.validation_id)
        if not self._identity_matches(latest, identity):
            raise PaperValidationError("experiment_identity_mismatch")
        row = PaperValidationPostmortemReview(
            validation_id=identity.validation_id,
            validation_version=identity.validation_version,
            experiment_id=identity.experiment_id,
            strategy_version_id=identity.strategy_version_id,
            cohort_id=identity.cohort_id,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            evaluator_id=actor.actor_id,
            evaluator_role=actor.role.value,
            review_text=request.review_text,
            cited_evidence=list(request.cited_evidence),
            experiment_hash=identity.experiment_hash,
            cohort_hash=identity.cohort_hash,
            strategy_hash=identity.strategy_hash,
            config_hash=identity.config_hash,
            policy_hash=identity.policy_hash,
            input_hash=identity.input_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def authorize_order_submission(
        self, caller_id: str, identity: ValidationIdentity
    ) -> PaperOrderAuthorization:
        actor = await self._resolve_actor(caller_id)
        self._require_role(actor, _MUTATION_ROLES)
        observed = await self._trusted_current(identity.validation_id)
        if not self._identity_matches(observed, identity):
            raise PaperValidationError("authorization_identity_mismatch")
        if not is_order_authorizable(ValidationState(observed.new_state)):
            raise PaperValidationError("order_state_not_authorized")
        frozen, policy = await self._resolve_evidence(identity)

        await self._lock_validation(identity.validation_id)
        latest = await self._trusted_current(identity.validation_id)
        if not self._identity_matches(latest, identity):
            raise PaperValidationError("authorization_identity_mismatch")
        state = ValidationState(latest.new_state)
        if not is_order_authorizable(state):
            raise PaperValidationError("order_state_not_authorized")
        return PaperOrderAuthorization(
            identity=identity,
            state=state,
            actor=actor,
            authorization_id=canonical_sha256(
                {
                    "identity": identity.model_dump(mode="python"),
                    "state": state,
                    "actor": actor.model_dump(mode="python"),
                    "input_bundle_id": frozen.bundle_id,
                    "policy_version": policy.version,
                }
            ),
        )

    async def confirm_promotion(
        self, caller_id: str, confirmation: PromotionConfirmationInput
    ) -> PaperValidationStateTransition:
        request = TransitionRequest(
            identity=confirmation.identity,
            expected_prior_state=ValidationState.PROMOTION_ELIGIBLE,
            target_state=ValidationState.PROMOTED,
            idempotency_key=confirmation.idempotency_key,
            reason_code="promotion_confirmed",
            reason_text=confirmation.reason,
            evidence_ids=confirmation.evidence_ids,
        )
        try:
            return await self._append_transition(
                caller_id,
                request,
                promotion_confirmed=confirmation.confirmed,
            )
        except PaperValidationError as exc:
            if exc.reason_code in {
                "experiment_identity_mismatch",
                "concurrent_transition_conflict",
                "invalid_transition",
                "terminal_state",
            }:
                raise PaperValidationError("promotion_confirmation_mismatch") from exc
            raise

    async def get_audit(self, caller_id: str, validation_id: str) -> dict[str, list]:
        await self._resolve_actor(caller_id)
        await self._lock_validation(validation_id)
        transitions = await self.get_history(caller_id, validation_id)
        hypotheses = list(
            (
                await self._session.execute(
                    select(StrategyHypothesisDraft)
                    .where(StrategyHypothesisDraft.validation_id == validation_id)
                    .order_by(
                        StrategyHypothesisDraft.created_at, StrategyHypothesisDraft.id
                    )
                )
            )
            .scalars()
            .all()
        )
        reviews = list(
            (
                await self._session.execute(
                    select(PaperValidationPostmortemReview)
                    .where(
                        PaperValidationPostmortemReview.validation_id == validation_id
                    )
                    .order_by(
                        PaperValidationPostmortemReview.created_at,
                        PaperValidationPostmortemReview.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "transitions": transitions,
            "hypotheses": hypotheses,
            "reviews": reviews,
        }

    async def get_history(
        self, caller_id: str, validation_id: str
    ) -> list[PaperValidationStateTransition]:
        await self._resolve_actor(caller_id)
        result = await self._session.execute(
            select(PaperValidationStateTransition)
            .where(PaperValidationStateTransition.validation_id == validation_id)
            .order_by(PaperValidationStateTransition.sequence)
        )
        return list(result.scalars().all())


__all__ = ["PaperValidationError", "PaperValidationService"]

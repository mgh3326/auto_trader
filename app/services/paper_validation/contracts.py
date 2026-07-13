"""Closed, side-effect-free contracts for ROB-848 paper validation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ActorRole(StrEnum):
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
    SYSTEM = "system"


class ValidationState(StrEnum):
    DRAFT = "draft"
    OFFLINE_ELIGIBLE = "offline_eligible"
    SHADOW_SOAK = "shadow_soak"
    PAPER_ACTIVE = "paper_active"
    PROMOTION_ELIGIBLE = "promotion_eligible"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ABORTED = "aborted"


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActorIdentity(FrozenContract):
    actor_id: NonBlank
    role: ActorRole


class ValidationIdentity(FrozenContract):
    validation_id: NonBlank
    validation_version: int = Field(ge=1)
    experiment_id: Sha256
    strategy_version_id: NonBlank
    cohort_id: NonBlank
    experiment_hash: Sha256
    cohort_hash: Sha256
    strategy_hash: Sha256
    config_hash: Sha256
    policy_hash: Sha256
    input_hash: Sha256

    @model_validator(mode="after")
    def experiment_id_is_canonical_hash(self) -> ValidationIdentity:
        if self.experiment_id != self.experiment_hash:
            raise ValueError("experiment_hash must exactly match experiment_id")
        return self


class FrozenInputStamp(FrozenContract):
    bundle_id: NonBlank
    content_hash: Sha256
    verified: Literal[True]


class PolicyStamp(FrozenContract):
    version: NonBlank
    content_hash: Sha256
    verified: Literal[True]


class ActorRoleProvider(Protocol):
    async def resolve(self, caller_id: str) -> ActorIdentity: ...


class FrozenInputHashProvider(Protocol):
    async def get_stamp(self, identity: ValidationIdentity) -> FrozenInputStamp: ...


class PolicyHashProvider(Protocol):
    async def get_stamp(self, identity: ValidationIdentity) -> PolicyStamp: ...


class TransitionRequest(FrozenContract):
    identity: ValidationIdentity
    expected_prior_state: ValidationState | None
    target_state: ValidationState
    idempotency_key: NonBlank
    reason_code: NonBlank
    reason_text: NonBlank
    evidence_ids: tuple[NonBlank, ...] = ()


class TransitionDecision(FrozenContract):
    allowed: bool
    reason_code: str | None = None


class HypothesisDraftInput(FrozenContract):
    validation_id: NonBlank
    idempotency_key: NonBlank
    mechanism: NonBlank
    universe: tuple[NonBlank, ...] = Field(min_length=1)
    horizon: NonBlank
    entry_criteria: tuple[NonBlank, ...] = Field(min_length=1)
    exit_criteria: tuple[NonBlank, ...] = Field(min_length=1)
    invalidation_criteria: tuple[NonBlank, ...] = Field(min_length=1)
    data_requirements: tuple[NonBlank, ...] = Field(min_length=1)
    expected_cost_hurdle: Decimal = Field(ge=0)
    turnover_bound: Decimal = Field(gt=0)
    risk_bound: Decimal = Field(gt=0)
    cited_evidence: tuple[NonBlank, ...] = Field(min_length=1)


class PostmortemReviewInput(FrozenContract):
    validation_id: NonBlank
    idempotency_key: NonBlank
    review_text: NonBlank
    cited_evidence: tuple[NonBlank, ...] = Field(min_length=1)


class PromotionConfirmationInput(FrozenContract):
    identity: ValidationIdentity
    idempotency_key: NonBlank
    reason: NonBlank
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)
    confirmed: Literal[True] = True


class PaperOrderAuthorization(FrozenContract):
    identity: ValidationIdentity
    state: ValidationState
    actor: ActorIdentity
    authorization_id: NonBlank
    authorized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "ActorIdentity",
    "ActorRole",
    "ActorRoleProvider",
    "FrozenInputHashProvider",
    "FrozenInputStamp",
    "HypothesisDraftInput",
    "PaperOrderAuthorization",
    "PolicyHashProvider",
    "PolicyStamp",
    "PostmortemReviewInput",
    "PromotionConfirmationInput",
    "Sha256",
    "TransitionDecision",
    "TransitionRequest",
    "ValidationIdentity",
    "ValidationState",
]

"""Application handlers for the ROB-848 paper-validation MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from sqlalchemy.inspection import inspect as sqlalchemy_inspect

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    ActorRoleProvider,
    HypothesisDraftInput,
    PostmortemReviewInput,
    PromotionConfirmationInput,
    TransitionRequest,
    ValidationIdentity,
    ValidationState,
)
from app.services.paper_validation.service import (
    PaperValidationError,
    PaperValidationService,
)


class PaperValidationApplication(Protocol):
    """Application boundary consumed by the MCP registration layer."""

    async def register(self, caller_id: str, request: TransitionRequest) -> object: ...

    async def advance(self, caller_id: str, request: TransitionRequest) -> object: ...

    async def append_hypothesis(
        self, caller_id: str, request: HypothesisDraftInput
    ) -> object: ...

    async def append_review(
        self, caller_id: str, request: PostmortemReviewInput
    ) -> object: ...

    async def get_audit(self, caller_id: str, validation_id: str) -> object: ...

    async def authorize_order_submit(
        self, caller_id: str, identity: ValidationIdentity
    ) -> object: ...

    async def confirm_promotion(
        self, caller_id: str, confirmation: PromotionConfirmationInput
    ) -> object: ...

    async def reject_or_abort(
        self, caller_id: str, request: TransitionRequest
    ) -> object: ...


ApplicationProvider = Callable[[], PaperValidationApplication]


class ConfiguredActorRoleProvider(ActorRoleProvider):
    """Resolve only authenticated request identities from operator configuration."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve(self, caller_id: str) -> ActorIdentity:
        try:
            role = ActorRole(self._mapping[caller_id])
        except (KeyError, ValueError) as exc:
            raise LookupError("caller role mapping unavailable") from exc
        return ActorIdentity(actor_id=caller_id, role=role)


def jsonable(value: object) -> Any:
    """Convert application results, including ORM rows, to MCP-safe values."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    try:
        mapper = sqlalchemy_inspect(type(value))
    except Exception:
        return value
    return {
        column.key: jsonable(getattr(value, column.key)) for column in mapper.columns
    }


class DefaultPaperValidationApplication:
    """DB composition with role mapping and deliberately absent ROB-849 providers."""

    def _service(self, session) -> PaperValidationService:  # noqa: ANN001, ANN202
        return PaperValidationService(
            session,
            actor_role_provider=ConfiguredActorRoleProvider(
                settings.PAPER_VALIDATION_ACTOR_ROLES
            ),
            frozen_input_provider=None,
            policy_provider=None,
        )

    async def _mutate(self, method: str, caller_id: str, request: object) -> object:
        try:
            async with AsyncSessionLocal() as session, session.begin():
                result = await getattr(self._service(session), method)(
                    caller_id, request
                )
                return jsonable(result)
        except PaperValidationError as exc:
            return {"status": "blocked", "reason_code": exc.reason_code}

    async def register(self, caller_id: str, request: TransitionRequest) -> object:
        if request.target_state is not ValidationState.DRAFT:
            return {"status": "blocked", "reason_code": "invalid_transition"}
        return await self._mutate("transition", caller_id, request)

    async def advance(self, caller_id: str, request: TransitionRequest) -> object:
        return await self._mutate("transition", caller_id, request)

    async def append_hypothesis(
        self, caller_id: str, request: HypothesisDraftInput
    ) -> object:
        return await self._mutate("append_hypothesis", caller_id, request)

    async def append_review(
        self, caller_id: str, request: PostmortemReviewInput
    ) -> object:
        return await self._mutate("append_postmortem_review", caller_id, request)

    async def get_audit(self, caller_id: str, validation_id: str) -> object:
        try:
            async with AsyncSessionLocal() as session, session.begin():
                result = await self._service(session).get_audit(
                    caller_id, validation_id
                )
                return jsonable(result)
        except PaperValidationError as exc:
            return {"status": "blocked", "reason_code": exc.reason_code}

    async def authorize_order_submit(
        self, caller_id: str, identity: ValidationIdentity
    ) -> object:
        return await self._mutate("authorize_order_submission", caller_id, identity)

    async def confirm_promotion(
        self, caller_id: str, confirmation: PromotionConfirmationInput
    ) -> object:
        return await self._mutate("confirm_promotion", caller_id, confirmation)

    async def reject_or_abort(
        self, caller_id: str, request: TransitionRequest
    ) -> object:
        if request.target_state not in {
            ValidationState.REJECTED,
            ValidationState.ABORTED,
        }:
            return {"status": "blocked", "reason_code": "invalid_transition"}
        return await self._mutate("transition", caller_id, request)


def default_application_provider() -> PaperValidationApplication:
    """Build the production paper-validation application composition."""
    return DefaultPaperValidationApplication()


__all__ = [
    "ApplicationProvider",
    "ConfiguredActorRoleProvider",
    "DefaultPaperValidationApplication",
    "PaperValidationApplication",
    "default_application_provider",
    "jsonable",
]

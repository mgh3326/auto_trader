"""Independent MCP registration for the ROB-848 validation boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.inspection import inspect as sqlalchemy_inspect

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.caller_identity import get_caller_agent_id
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

if TYPE_CHECKING:
    from fastmcp import FastMCP


PAPER_VALIDATION_TOOL_NAMES: set[str] = {
    "paper_validation_register",
    "paper_validation_advance",
    "paper_validation_append_hypothesis",
    "paper_validation_append_review",
    "paper_validation_get_audit",
    "paper_validation_authorize_order_submit",
    "paper_validation_confirm_promotion",
    "paper_validation_reject_or_abort",
}
PAPER_VALIDATION_MUTATION_TOOL_NAMES = PAPER_VALIDATION_TOOL_NAMES - {
    "paper_validation_get_audit"
}


class _PaperValidationApplication(Protocol):
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


ApplicationProvider = Callable[[], _PaperValidationApplication]


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


def _jsonable(value: object) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
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
        column.key: _jsonable(getattr(value, column.key)) for column in mapper.columns
    }


class _DefaultPaperValidationApplication:
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
                return _jsonable(result)
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
            async with AsyncSessionLocal() as session:
                result = await self._service(session).get_audit(
                    caller_id, validation_id
                )
                return _jsonable(result)
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


def _default_application_provider() -> _PaperValidationApplication:
    return _DefaultPaperValidationApplication()


def register_paper_validation_tools(
    mcp: FastMCP,
    *,
    application_provider: ApplicationProvider | None = None,
) -> None:
    """Register validation beside, but independently of, the broker façade."""
    application = (application_provider or _default_application_provider)()

    def caller_id() -> str | None:
        return get_caller_agent_id()

    def unavailable() -> dict[str, str]:
        return {"status": "blocked", "reason_code": "actor_identity_unavailable"}

    @mcp.tool(
        name="paper_validation_register",
        description="Register an exact-bound paper validation in draft state.",
    )
    async def paper_validation_register(request: TransitionRequest) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.register(caller, request))
        )

    @mcp.tool(
        name="paper_validation_advance",
        description="Append one legal deterministic paper-validation transition.",
    )
    async def paper_validation_advance(request: TransitionRequest) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.advance(caller, request))
        )

    @mcp.tool(
        name="paper_validation_append_hypothesis",
        description="Append an immutable researcher-authored hypothesis draft.",
    )
    async def paper_validation_append_hypothesis(
        request: HypothesisDraftInput,
    ) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.append_hypothesis(caller, request))
        )

    @mcp.tool(
        name="paper_validation_append_review",
        description="Append an immutable reviewer-authored postmortem narrative.",
    )
    async def paper_validation_append_review(
        request: PostmortemReviewInput,
    ) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.append_review(caller, request))
        )

    @mcp.tool(
        name="paper_validation_get_audit",
        description="Read complete ordered validation, hypothesis, and review audit.",
    )
    async def paper_validation_get_audit(validation_id: str) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.get_audit(caller, validation_id))
        )

    @mcp.tool(
        name="paper_validation_authorize_order_submit",
        description="Authorize exact-bound paper submission without submitting an order.",
    )
    async def paper_validation_authorize_order_submit(
        identity: ValidationIdentity,
    ) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.authorize_order_submit(caller, identity))
        )

    @mcp.tool(
        name="paper_validation_confirm_promotion",
        description="Explicitly confirm promotion against current frozen identity.",
    )
    async def paper_validation_confirm_promotion(
        confirmation: PromotionConfirmationInput,
    ) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.confirm_promotion(caller, confirmation))
        )

    @mcp.tool(
        name="paper_validation_reject_or_abort",
        description="Append a terminal rejection or abort from promotion eligibility.",
    )
    async def paper_validation_reject_or_abort(
        request: TransitionRequest,
    ) -> dict[str, Any]:
        caller = caller_id()
        return (
            unavailable()
            if caller is None
            else _jsonable(await application.reject_or_abort(caller, request))
        )


__all__ = [
    "ApplicationProvider",
    "ConfiguredActorRoleProvider",
    "PAPER_VALIDATION_MUTATION_TOOL_NAMES",
    "PAPER_VALIDATION_TOOL_NAMES",
    "register_paper_validation_tools",
]

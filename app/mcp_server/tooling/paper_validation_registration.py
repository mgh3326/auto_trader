"""Independent MCP registration for the ROB-848 validation boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.mcp_server.tooling.paper_validation_handlers import (
    ApplicationProvider,
    ConfiguredActorRoleProvider,
    default_application_provider,
    jsonable,
)
from app.services.paper_validation.contracts import (
    HypothesisDraftInput,
    PostmortemReviewInput,
    PromotionConfirmationInput,
    TransitionRequest,
    ValidationIdentity,
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


def register_paper_validation_tools(
    mcp: FastMCP,
    *,
    application_provider: ApplicationProvider | None = None,
) -> None:
    """Register validation beside, but independently of, the broker façade."""
    application = (application_provider or default_application_provider)()

    def caller_id() -> str | None:
        actor_id = settings.PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID.strip()
        return actor_id or None

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
            else jsonable(await application.register(caller, request))
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
            else jsonable(await application.advance(caller, request))
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
            else jsonable(await application.append_hypothesis(caller, request))
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
            else jsonable(await application.append_review(caller, request))
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
            else jsonable(await application.get_audit(caller, validation_id))
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
            else jsonable(await application.authorize_order_submit(caller, identity))
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
            else jsonable(await application.confirm_promotion(caller, confirmation))
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
            else jsonable(await application.reject_or_abort(caller, request))
        )


__all__ = [
    "ApplicationProvider",
    "ConfiguredActorRoleProvider",
    "PAPER_VALIDATION_MUTATION_TOOL_NAMES",
    "PAPER_VALIDATION_TOOL_NAMES",
    "register_paper_validation_tools",
]

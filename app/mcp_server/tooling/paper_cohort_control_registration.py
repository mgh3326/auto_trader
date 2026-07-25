"""Exact PAPER_EXECUTION-only registration for the ROB-849 kill switch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.mcp_server.tooling.paper_cohort_control_handlers import (
    ApplicationProvider,
    default_application_provider,
)
from app.mcp_server.tooling.paper_validation_handlers import jsonable
from app.services.paper_cohort.contracts import PaperCohortKillRequest

if TYPE_CHECKING:
    from fastmcp import FastMCP


PAPER_COHORT_CONTROL_TOOL_NAMES: set[str] = {"paper_cohort_kill_switch"}


def register_paper_cohort_control_tools(
    mcp: FastMCP,
    *,
    application_provider: ApplicationProvider | None = None,
) -> None:
    application = (application_provider or default_application_provider)()

    @mcp.tool(
        name="paper_cohort_kill_switch",
        description=(
            "Durably fence one paper cohort, recover/link prepared native orders "
            "without POST, then cancel/close proven cohort-owned exposure."
        ),
    )
    async def paper_cohort_kill_switch(
        request: PaperCohortKillRequest,
    ) -> dict[str, Any]:
        caller_id = settings.PAPER_VALIDATION_AUTHENTICATED_ACTOR_ID.strip()
        if not caller_id:
            return {
                "status": "blocked",
                "reason_code": "actor_identity_unavailable",
            }
        return jsonable(await application.kill_switch(caller_id, request))


__all__ = [
    "PAPER_COHORT_CONTROL_TOOL_NAMES",
    "register_paper_cohort_control_tools",
]

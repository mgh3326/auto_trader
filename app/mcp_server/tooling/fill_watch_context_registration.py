"""Closed-world MCP registration for #137's context-only artifact boundary.

The profile is a physical tool boundary, not a prompt convention. It exposes
only one default-off artifact write and one outcome read. The functions do not
import account, broker, OAuth, proposal, watch-mutation, scheduler, shell, or
capability modules.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from app.services.fill_watch_context.consumer import (
    EVENT_LOOP_FLAG,
    EventLoopDisabled,
    consume_once_if_armed,
)
from app.services.fill_watch_context.repository import FillWatchContextOutcomeRepository
from app.services.fill_watch_context.service import FillWatchContextOutcomeService

if TYPE_CHECKING:
    from fastmcp import FastMCP

FILL_WATCH_CONTEXT_TOOL_NAMES = frozenset(
    {
        "fill_watch_context_consume_artifact",
        "fill_watch_context_outcome_get",
    }
)

__all__ = [
    "FILL_WATCH_CONTEXT_TOOL_NAMES",
    "ContextProfileSurfaceViolation",
    "assert_provisioned_surface",
    "build_fill_watch_context_server",
    "provisioned_tool_names",
    "register_fill_watch_context_tools",
]


class ContextProfileSurfaceViolation(RuntimeError):
    """The server's actually served closed-world tool set drifted."""


def register_fill_watch_context_tools(mcp: FastMCP) -> None:
    """Register precisely the supplied-artifact write and outcome read tools."""

    @mcp.tool(
        name="fill_watch_context_consume_artifact",
        description=(
            "Default-off, context-only consumption of one supplied lane.event "
            "artifact. It records no economic decision."
        ),
    )
    async def fill_watch_context_consume_artifact(
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        # Import settings only at invocation. Building/listing this profile is
        # inert and opens neither a DB session nor an external connection.
        from app.core.config import settings

        try:
            receipt = await consume_once_if_armed(
                artifact,
                settings_obj=settings,
            )
        except EventLoopDisabled:
            return {
                "delivery_ack": {
                    "accepted": False,
                    "persisted": False,
                    "reason": f"{EVENT_LOOP_FLAG} is false",
                },
                "consumption": None,
            }
        return receipt.as_dict()

    @mcp.tool(
        name="fill_watch_context_outcome_get",
        description=(
            "Read one non-economic context outcome by canonical transport UUID."
        ),
    )
    async def fill_watch_context_outcome_get(
        transport_event_uuid: str,
    ) -> dict[str, Any]:
        event_uuid = uuid.UUID(transport_event_uuid)
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            service = FillWatchContextOutcomeService(
                FillWatchContextOutcomeRepository(session)
            )
            outcome = await service.get(str(event_uuid))
        return {
            "found": outcome is not None,
            "outcome": outcome.as_dict() if outcome else None,
        }


def build_fill_watch_context_server(
    *, name: str = "auto_trader-context-mcp"
) -> FastMCP:
    """Build an unstarted server through the real profile registry branch."""
    from fastmcp import FastMCP as _FastMCP

    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling.registry import register_all_tools

    mcp = _FastMCP(name=name, on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.FILL_WATCH_CONTEXT)
    return cast("FastMCP", mcp)


async def provisioned_tool_names(mcp: Any) -> frozenset[str]:
    """Ask the real server's ``tools/list`` surface rather than a stand-in."""
    lister = getattr(mcp, "list_tools", None)
    if lister is None:
        raise TypeError("server object does not serve tools/list")
    return frozenset(str(tool.name) for tool in await lister())


async def assert_provisioned_surface(mcp: Any) -> frozenset[str]:
    """Refuse both missing and extra tools at the serving boundary."""
    served = await provisioned_tool_names(mcp)
    if served != FILL_WATCH_CONTEXT_TOOL_NAMES:
        raise ContextProfileSurfaceViolation(
            "fill/watch context MCP surface mismatch: "
            f"missing={sorted(FILL_WATCH_CONTEXT_TOOL_NAMES - served)}, "
            f"extra={sorted(served - FILL_WATCH_CONTEXT_TOOL_NAMES)}"
        )
    return served

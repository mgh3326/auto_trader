"""MCP registration for deterministic proposal revalidation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.proposal_revalidate import proposal_revalidate_impl

if TYPE_CHECKING:
    from fastmcp import FastMCP

PROPOSAL_REVALIDATE_TOOL_NAMES = {"proposal_revalidate"}


def register_proposal_revalidate_tools(
    mcp: FastMCP,
    *,
    registered_tool_names: Callable[[], set[str] | Awaitable[set[str]]],
) -> None:
    """Register the label surface with served-inventory profile gates."""

    @mcp.tool(
        name="proposal_revalidate",
        description=(
            "Label active proposals against current quotes, durable terminal evidence, "
            "and the current policy stamp. Dry run is the default and writes nothing. "
            "A confirmed non-dry run asks the existing narrow void surface only for "
            "terminal-evidence labels; it never changes an active proposal."
        ),
    )
    async def proposal_revalidate(
        market: str,
        proposal_ids: list[str] | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await proposal_revalidate_impl(
            market,
            proposal_ids,
            dry_run,
            confirm,
            registered_tool_names=registered_tool_names,
        )


__all__ = ["PROPOSAL_REVALIDATE_TOOL_NAMES", "register_proposal_revalidate_tools"]

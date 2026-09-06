"""MCP registration for the read-only session bootstrap pack."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.session_bootstrap_pack import _session_bootstrap_pack

if TYPE_CHECKING:
    from fastmcp import FastMCP

SESSION_BOOTSTRAP_TOOL_NAMES = {"session_bootstrap_pack"}


def _tool_names(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(name) for name in value}
    if isinstance(value, (list, tuple, set, frozenset)):
        names: set[str] = set()
        for tool in value:
            name = getattr(tool, "name", tool)
            if not isinstance(name, str):
                raise TypeError("registered tool has no string name")
            names.add(name)
        return names
    raise TypeError("registered tool inventory has an unsupported shape")


async def registered_tool_names_for(mcp: Any) -> set[str]:
    """Resolve the served registration inventory in the prescribed order."""

    tools = getattr(mcp, "tools", None)
    if isinstance(tools, Mapping):
        return _tool_names(tools)

    lister = getattr(mcp, "list_tools", None)
    if callable(lister):
        listed = lister()
        if inspect.isawaitable(listed):
            listed = await listed
        return _tool_names(listed)

    inner = getattr(mcp, "_inner", None)
    inner_tools = getattr(inner, "tools", None)
    if isinstance(inner_tools, Mapping):
        return _tool_names(inner_tools)
    raise TypeError("MCP registration inventory is unavailable")


def register_session_bootstrap_tools(
    mcp: FastMCP,
    *,
    registered_tool_names: Callable[[], set[str] | Awaitable[set[str]]],
) -> None:
    """Register a read-only pack whose section visibility follows the profile."""

    @mcp.tool(
        name="session_bootstrap_pack",
        description=(
            "Read-only, no-mutation session bootstrap pack for briefing, holdings, "
            "cash, resting proposals, pending retrospectives, due forecasts, "
            "policy, and recent context. Every section is sourced from its existing "
            "tool response; unavailable profile sections are reported explicitly. "
            "Responses use deterministic compact truncation when requested or above "
            "the response cap. Forecast resolution is fixed to dry_run=true."
        ),
    )
    async def session_bootstrap_pack(
        market: str,
        include: list[str] | None = None,
        compact: bool = False,
    ) -> dict[str, Any]:
        return await _session_bootstrap_pack(
            market,
            include,
            compact,
            registered_tool_names=registered_tool_names,
        )


__all__ = [
    "SESSION_BOOTSTRAP_TOOL_NAMES",
    "register_session_bootstrap_tools",
    "registered_tool_names_for",
]

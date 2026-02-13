from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


_MCP_REGISTERED_TOOL_NAMES: dict[int, set[str]] = {}


@dataclass
class _FilteredMCP:
    """Wrapper that registers only selected tool names for a given MCP instance."""

    mcp: object
    allowed_names: set[str]

    def tool(self, name: str, description: str):
        mcp_id = id(self.mcp)
        registered = _MCP_REGISTERED_TOOL_NAMES.setdefault(mcp_id, set())

        if name in self.allowed_names and name not in registered:
            registered.add(name)

            # type: ignore[union-attr]
            return self.mcp.tool(name=name, description=description)

        def noop(func):
            return func

        return noop


def _register_tools_once(mcp: FastMCP, tool_names: Iterable[str]) -> None:
    from app.mcp_server.tools import register_tools

    filtered = _FilteredMCP(mcp=mcp, allowed_names=set(tool_names))
    register_tools(filtered)

def register_tool_subset(mcp: FastMCP, tool_names: Iterable[str]) -> None:
    _register_tools_once(mcp, tool_names)


__all__ = [
    "register_tool_subset",
]

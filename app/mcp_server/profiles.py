"""MCP server profile definitions.

Profiles gate which tool subsets are registered at startup.
Profile selection is driven by the MCP_PROFILE env var (default: "default").
"""

from __future__ import annotations

from enum import StrEnum


class McpProfile(StrEnum):
    DEFAULT = "default"
    HERMES_PAPER_KIS = "hermes-paper-kis"


def resolve_mcp_profile(env: str | None) -> McpProfile:
    """Resolve MCP_PROFILE env value to McpProfile.

    Empty/None → DEFAULT. Invalid string → ValueError.
    """
    normalized = (env or "").strip()
    if not normalized:
        return McpProfile.DEFAULT
    try:
        return McpProfile(normalized)
    except ValueError:
        allowed = ", ".join(f'"{p}"' for p in McpProfile)
        raise ValueError(
            f"Unknown MCP_PROFILE '{normalized}'; allowed values: {allowed}"
        )


__all__ = ["McpProfile", "resolve_mcp_profile"]

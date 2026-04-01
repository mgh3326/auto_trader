from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.user_settings_tools import (
    get_user_setting,
    set_user_setting,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

USER_SETTINGS_TOOL_NAMES: set[str] = {
    "get_user_setting",
    "set_user_setting",
}


def register_user_settings_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_user_setting",
        description=(
            "Get a user setting value by key. "
            "Returns the JSON value if found, None otherwise."
        ),
    )(get_user_setting)
    _ = mcp.tool(
        name="set_user_setting",
        description=(
            "Set a user setting value by key (upsert). "
            "Creates or updates the setting and returns the serialized result with key, value, and updated_at."
        ),
    )(set_user_setting)


__all__ = [
    "USER_SETTINGS_TOOL_NAMES",
    "register_user_settings_tools",
]

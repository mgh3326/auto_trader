from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.user_settings_tools import (
    get_parking_exclusion,
    get_user_setting,
    set_user_setting,
)
from app.services.manual_cash_settings import MANUAL_CASH_MAX_KRW
from app.services.parking_exclusion_settings import PARKING_EXCLUSION_CURRENCIES

if TYPE_CHECKING:
    from fastmcp import FastMCP

USER_SETTINGS_TOOL_NAMES: set[str] = {
    "get_user_setting",
    "set_user_setting",
}

# #883 — the typed parking_exclusion read is a separate, narrower tool name so
# the generic USER_SETTINGS_TOOL_NAMES pair is not widened by it.
PARKING_EXCLUSION_TOOL_NAMES: set[str] = {"get_parking_exclusion"}


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
            "Supported operator-maintained keys include 'manual_cash' and 'account_costs'. "
            "For 'manual_cash' the value must be "
            f'{{"amount": <whole KRW 0..{MANUAL_CASH_MAX_KRW}>}} '
            '(optional "accounts" [{name, amount}] summing to amount); invalid values '
            "are rejected and the stored source is always 'mcp_set_user_setting'. "
            "For 'parking_exclusion' the value must be an object with keys a "
            f"subset of {list(PARKING_EXCLUSION_CURRENCIES)} and non-negative "
            "finite amounts; invalid values are rejected. "
            "Creates or updates the setting and returns the serialized result with key, value, and updated_at."
        ),
    )(set_user_setting)


def register_parking_exclusion_tool(mcp: FastMCP) -> None:
    """Register the typed parking_exclusion read (#883).

    Deliberately separate from register_user_settings_tools: the generic
    get/set pair is broad-profile surface, while this read is exposed only on
    the profiles/lanes that run the cash sweep.
    """
    _ = mcp.tool(
        name="get_parking_exclusion",
        description=(
            "Read the operator-set 'parking_exclusion' user setting for the "
            "cash sweep: per-currency amounts to leave unparked. Returns "
            'status "ok" with exclusions {"KRW": "...", "USD": "..."} as '
            "exact decimal strings (absent setting or absent currency = "
            '"0"), or status "unknown" with exclusions=null when the stored '
            "value is malformed or unreadable — park nothing on 'unknown', "
            "it is never equivalent to 0."
        ),
    )(get_parking_exclusion)


__all__ = [
    "PARKING_EXCLUSION_TOOL_NAMES",
    "USER_SETTINGS_TOOL_NAMES",
    "register_parking_exclusion_tool",
    "register_user_settings_tools",
]

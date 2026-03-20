from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trade_profile_tools import (
    delete_asset_profile,
    get_asset_profile,
    get_market_filters,
    get_tier_rule_params,
    set_asset_profile,
    set_market_filter,
    set_tier_rule_params,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_PROFILE_TOOL_NAMES: set[str] = {
    "get_asset_profile",
    "set_asset_profile",
    "get_tier_rule_params",
    "set_tier_rule_params",
    "get_market_filters",
    "set_market_filter",
    "delete_asset_profile",
}


def register_trade_profile_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_asset_profile",
        description=(
            "Get asset profiles filtered by symbol, market_type (kr/us/crypto), profile, tier. "
            "tier must be 1-4; profile must be aggressive/balanced/conservative/exit/hold_only. "
            "Invalid market_type returns an error. "
            "Set include_rules=True to also fetch tier rule parameters."
        ),
    )(get_asset_profile)
    _ = mcp.tool(
        name="set_asset_profile",
        description=(
            "Create or update an asset profile. "
            "market_type accepts kr/us/crypto; invalid values return an error. "
            "New profiles require market_type, tier (1-4), and profile "
            "(aggressive/balanced/conservative/exit/hold_only). "
            "profile=exit forces buy_allowed=False. "
            "profile=hold_only forces sell_mode=rebalance_only."
        ),
    )(set_asset_profile)
    _ = mcp.tool(
        name="get_tier_rule_params",
        description=(
            "Get tier rule params. "
            "Filter by instrument_type (kr/us/crypto), tier (1-4), profile, "
            "param_type (buy/sell/stop/rebalance/common)."
        ),
    )(get_tier_rule_params)
    _ = mcp.tool(
        name="set_tier_rule_params",
        description=(
            "Upsert tier rule params. "
            "Requires instrument_type, tier (1-4), profile, param_type, and params dict. "
            "Updates version on each edit."
        ),
    )(set_tier_rule_params)
    _ = mcp.tool(
        name="get_market_filters",
        description=(
            "Get market filters. Filter by instrument_type and enabled_only flag."
        ),
    )(get_market_filters)
    _ = mcp.tool(
        name="set_market_filter",
        description=(
            "Create or update a market filter. "
            "Requires instrument_type, filter_name, params dict."
        ),
    )(set_market_filter)
    _ = mcp.tool(
        name="delete_asset_profile",
        description=("Delete an asset profile by symbol. Logs to profile_change_log."),
    )(delete_asset_profile)


__all__ = [
    "TRADE_PROFILE_TOOL_NAMES",
    "register_trade_profile_tools",
]

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trade_profile_tools import (
    delete_asset_profile,
    get_asset_profile,
    get_market_filters,
    get_tier_rule_params,
    prepare_trade_draft,
    set_asset_profile,
    set_market_filter,
    set_tier_rule_params,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_PROFILE_TOOL_NAMES: set[str] = {
    "delete_asset_profile",
    "get_asset_profile",
    "get_market_filters",
    "get_tier_rule_params",
    "prepare_trade_draft",
    "set_asset_profile",
    "set_market_filter",
    "set_tier_rule_params",
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
            "Get tier rule params filtered by instrument_type (kr/us/crypto aliases accepted), "
            "tier, profile, and param_type."
        ),
    )(get_tier_rule_params)
    _ = mcp.tool(
        name="set_tier_rule_params",
        description=(
            "Create or update tier rule params. instrument_type accepts kr/us/crypto aliases; "
            "tier must be 1-4; profile and param_type must be valid trade-profile values."
        ),
    )(set_tier_rule_params)
    _ = mcp.tool(
        name="get_market_filters",
        description=(
            "Get market filters filtered by instrument_type (kr/us/crypto aliases accepted), "
            "filter_name, and enabled status."
        ),
    )(get_market_filters)
    _ = mcp.tool(
        name="set_market_filter",
        description=(
            "Create or update a market filter row. instrument_type accepts kr/us/crypto aliases; "
            "filter_name stays snake_case and params are stored as JSON."
        ),
    )(set_market_filter)
    _ = mcp.tool(
        name="delete_asset_profile",
        description=(
            "Delete an asset profile by symbol with optional market_type (kr/us/crypto aliases accepted). "
            "Returns action='deleted' and records an audit snapshot."
        ),
    )(delete_asset_profile)
    _ = mcp.tool(
        name="prepare_trade_draft",
        description=(
            "Prepare deterministic trade drafts grouped by market using stored profiles, tier rules, "
            "and market filters. instrument_type accepts kr/us/crypto aliases."
        ),
    )(prepare_trade_draft)


__all__ = [
    "TRADE_PROFILE_TOOL_NAMES",
    "register_trade_profile_tools",
]

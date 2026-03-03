from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trade_profile_tools import (
    get_asset_profile,
    set_asset_profile,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_PROFILE_TOOL_NAMES: set[str] = {"get_asset_profile", "set_asset_profile"}


def register_trade_profile_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_asset_profile",
        description=(
            "Get asset profiles filtered by symbol, market_type (kr/us/crypto), profile, tier. "
            "Set include_rules=True to also fetch tier rule parameters."
        ),
    )(get_asset_profile)
    _ = mcp.tool(
        name="set_asset_profile",
        description=(
            "Create or update an asset profile. "
            "market_type: kr/us/crypto. "
            "New profiles require market_type, tier, profile. "
            "profile=exit forces buy_allowed=False. "
            "profile=hold_only forces sell_mode=rebalance_only."
        ),
    )(set_asset_profile)


__all__ = [
    "TRADE_PROFILE_TOOL_NAMES",
    "register_trade_profile_tools",
]

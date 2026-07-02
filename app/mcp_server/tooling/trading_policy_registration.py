"""Registration for the read-only get_trading_policy MCP tool (ROB-646)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trading_policy_tools import get_trading_policy

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADING_POLICY_TOOL_NAMES: set[str] = {"get_trading_policy"}


def register_trading_policy_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_trading_policy",
        description=(
            "Read trading judgment thresholds for a market x lane from the "
            "single authoritative config/trading_policy.yaml. Args: "
            "market in {kr, us, crypto}, lane in {buy, sell, discovery} "
            "(sell = profit-taking). Returns resolved thresholds (value/unit/"
            "semantics/source) plus the policy version stamp "
            "{version, content_hash}. VERSION-STAMPING CONTRACT: cite this "
            "stamp when recording a verdict (report item evidence_snapshot, "
            "trade_retrospectives, forecast) so the criteria are recoverable. "
            "Unknown market/lane returns success=false, error=unknown_key. "
            "Read-only — the policy is edited by operator PR, never by a tool."
        ),
    )(get_trading_policy)


__all__ = [
    "TRADING_POLICY_TOOL_NAMES",
    "register_trading_policy_tools",
]

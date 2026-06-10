"""ROB-269 Phase 2 — MCP tool registration for the snapshot foundation.

Gated by ``settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED`` in ``registry.py``.
When the flag is off, this module is imported but
``register_investment_snapshots_tools`` is not called — the 3 read tools
are absent from the MCP surface (bundle_ensure/refresh_request were retired
in ROB-488).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.investment_snapshots_tools import (
    investment_snapshot_bundle_get,
    investment_snapshot_bundle_list,
    investment_snapshot_list,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


INVESTMENT_SNAPSHOTS_TOOL_NAMES: set[str] = {
    "investment_snapshot_bundle_get",
    "investment_snapshot_bundle_list",
    "investment_snapshot_list",
}


def register_investment_snapshots_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="investment_snapshot_bundle_get",
        description=(
            "Fetch one snapshot bundle by UUID with linked items. "
            "include_payload_preview=True adds up to 2KB of payload preview per item."
        ),
    )(investment_snapshot_bundle_get)

    _ = mcp.tool(
        name="investment_snapshot_bundle_list",
        description=(
            "List recent snapshot bundles (header only). Filters by purpose, market, "
            "account_scope, status. limit clamped to [1,100]."
        ),
    )(investment_snapshot_bundle_list)

    _ = mcp.tool(
        name="investment_snapshot_list",
        description=(
            "List recent snapshot metadata. Payload bodies are NOT returned in list "
            "view. Filters: market, symbol, snapshot_kind, source_kind, "
            "freshness_status, since (ISO-8601). limit clamped to [1,100]."
        ),
    )(investment_snapshot_list)


__all__ = [
    "INVESTMENT_SNAPSHOTS_TOOL_NAMES",
    "register_investment_snapshots_tools",
]

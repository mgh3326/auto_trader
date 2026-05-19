"""ROB-269 Phase 2 — MCP tool registration for the snapshot foundation.

Gated by ``settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED`` in ``registry.py``.
When the flag is off, this module is imported but
``register_investment_snapshots_tools`` is not called — the 4 tools are
absent from the MCP surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.investment_snapshots_tools import (
    investment_snapshot_bundle_ensure,
    investment_snapshot_bundle_get,
    investment_snapshot_bundle_list,
    investment_snapshot_list,
    investment_snapshot_refresh_request,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


INVESTMENT_SNAPSHOTS_TOOL_NAMES: set[str] = {
    "investment_snapshot_bundle_ensure",
    "investment_snapshot_bundle_get",
    "investment_snapshot_bundle_list",
    "investment_snapshot_list",
    "investment_snapshot_refresh_request",
}


def register_investment_snapshots_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="investment_snapshot_bundle_ensure",
        description=(
            "Ensure a snapshot bundle exists for (purpose, market, account_scope, "
            "policy_version). Reuses the latest bundle within bundle_ttl; otherwise "
            "creates a new run and bundle. Phase 2 has no production collectors — "
            "without a fresh bundle this typically returns status='failed'. Use "
            "investment_snapshot_refresh_request to ask the scheduler to refresh."
        ),
    )(investment_snapshot_bundle_ensure)

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

    _ = mcp.tool(
        name="investment_snapshot_refresh_request",
        description=(
            "Record a refresh request as an investment_snapshot_runs row. Inserts "
            "one row with purpose='manual_refresh' or 'reviewer_requested'; no "
            "collection happens in Phase 2 (the Phase 3 scheduler will pick it up)."
        ),
    )(investment_snapshot_refresh_request)


__all__ = [
    "INVESTMENT_SNAPSHOTS_TOOL_NAMES",
    "register_investment_snapshots_tools",
]

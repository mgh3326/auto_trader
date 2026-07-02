"""MCP registration for ROB-637 analysis artifact tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.analysis_artifact_tools import (
    analysis_artifact_get,
    analysis_artifact_list,
    analysis_artifact_save,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


ANALYSIS_ARTIFACT_TOOL_NAMES: set[str] = {
    "analysis_artifact_save",
    "analysis_artifact_list",
    "analysis_artifact_get",
}


def register_analysis_artifact_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="analysis_artifact_save",
        description=(
            "Persist a structured analysis artifact (screening ranking, "
            "profit-taking verdicts, support/resistance map, flow assessment, "
            "candidate pool, or session summary) for cross-session reuse. "
            "Explicit save only — analysis runs do not auto-persist."
        ),
    )(analysis_artifact_save)
    _ = mcp.tool(
        name="analysis_artifact_list",
        description=(
            "List persisted analysis artifacts, newest as_of first. "
            "Optional filters: market, kind, symbol (containment match on the "
            "symbols array), since, include_stale, limit clamped to 1..100. "
            "Stale rows (valid_until in the past) are excluded unless "
            "include_stale=true."
        ),
    )(analysis_artifact_list)
    _ = mcp.tool(
        name="analysis_artifact_get",
        description=(
            "Fetch a single analysis artifact by numeric id or artifact_uuid, "
            "including the full payload."
        ),
    )(analysis_artifact_get)


__all__ = [
    "ANALYSIS_ARTIFACT_TOOL_NAMES",
    "register_analysis_artifact_tools",
]

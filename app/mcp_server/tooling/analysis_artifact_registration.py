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
            "candidate pool, session summary, or briefing) for cross-session "
            "reuse. Explicit save only — analysis runs do not auto-persist. "
            "Cross-session artifact store — complementary to, not a duplicate "
            "of, the ROB-638 fetch-layer cache (which dedupes provider fetches "
            "within a run). Idempotent per correlation_id: re-saving the same "
            "correlation_id updates the row in place and bumps version "
            "(action='updated'); an identical payload is a no-op "
            "(action='unchanged', version preserved); omit correlation_id to "
            "append. content_hash is server-computed over the payload. When "
            "valid_until is omitted a per-kind default TTL is assigned so no "
            "artifact is never-stale. Optional readiness_label is advisory "
            "(screen_grade / not_decision_ready / ready_for_order_review / "
            "blocked). Payload capped at 100KB (payload_too_large above that). "
            "Recent valid artifacts are surfaced metadata-only in "
            "get_operating_briefing."
        ),
    )(analysis_artifact_save)
    _ = mcp.tool(
        name="analysis_artifact_list",
        description=(
            "List persisted analysis artifacts, newest as_of first — "
            "metadata only, no payload (payload_size_bytes hints the "
            "analysis_artifact_get cost). Optional filters: market, kind, "
            "symbol (containment match on the symbols array), since, "
            "correlation_id, account_scope, include_stale, limit clamped to "
            "1..100. Stale rows (valid_until in the past) are excluded unless "
            "include_stale=true; each row carries is_stale."
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

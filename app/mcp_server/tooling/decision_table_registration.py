"""MCP registration for the pure decision-table validator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.decision_table_validate import decision_table_validate

if TYPE_CHECKING:
    from fastmcp import FastMCP

DECISION_TABLE_TOOL_NAMES: set[str] = {"decision_table_validate"}

_TOOL_DESCRIPTION = (
    "Pure, read-only validation for kr-nxt-decision-table/v1.1 prep artifacts. "
    "It performs no database, network, broker, proposal, or order operation. "
    "A block violation means the table must be corrected before a downstream "
    "consumer may act on it."
)


def register_decision_table_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="decision_table_validate", description=_TOOL_DESCRIPTION)
    def validate(table: dict[str, Any], market: str) -> dict[str, Any]:
        return decision_table_validate(table, market)


__all__ = ["DECISION_TABLE_TOOL_NAMES", "register_decision_table_tools"]

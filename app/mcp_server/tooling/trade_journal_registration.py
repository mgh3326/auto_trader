# app/mcp_server/tooling/trade_journal_registration.py
"""MCP registration for trade journal tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.trade_journal_tools import (
    get_trade_journal,
    save_trade_journal,
    update_trade_journal,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_JOURNAL_TOOL_NAMES: set[str] = {
    "save_trade_journal",
    "get_trade_journal",
    "update_trade_journal",
}


def register_trade_journal_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="save_trade_journal",
        description=(
            "Save a trade journal entry with investment thesis and strategy metadata. "
            "Call this when recommending a buy/sell to record WHY. "
            "symbol auto-detects instrument_type. min_hold_days sets hold_until. "
            "status defaults to 'draft' — set to 'active' after fill confirmation. "
            "account_type='paper' for paper trading journals (requires account name). "
            "paper_trade_id links to the paper trade record."
        ),
    )(save_trade_journal)
    _ = mcp.tool(
        name="get_trade_journal",
        description=(
            "Query trade journals. MUST call before any sell recommendation to check "
            "thesis, hold period, target/stop prices. "
            "Returns active journals by default. "
            "Each entry includes hold_remaining_days and hold_expired. "
            "account_type defaults to 'live'; set to 'paper' for paper journals, "
            "or None to query both."
        ),
    )(get_trade_journal)
    _ = mcp.tool(
        name="update_trade_journal",
        description=(
            "Update a trade journal. Use for: "
            "draft->active (after fill), close (target reached), stop (stop-loss hit), "
            "or adjust target/stop/notes. "
            "Find by journal_id or symbol (latest active). "
            "Auto-calculates pnl_pct on close."
        ),
    )(update_trade_journal)


__all__ = [
    "TRADE_JOURNAL_TOOL_NAMES",
    "register_trade_journal_tools",
]

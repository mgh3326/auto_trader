# app/mcp_server/tooling/trade_journal_registration.py
"""MCP registration for trade journal tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.order_journal import (
    list_active_journals,
    modify_journal_entry,
)
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
    "list_active_journals",
    "modify_journal_entry",
}


def register_trade_journal_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="list_active_journals",
        description=(
            "List active trade journals for audit/planning. "
            "ROB-568: includes US FX rate fields for overseas equity journals."
        ),
    )
    async def list_active_journals_tool(
        symbol: str | None = None,
        account_type: str = "live",
        account: str | None = None,
        limit: int = 50,
    ):
        return await list_active_journals(
            symbol=symbol, account_type=account_type, account=account, limit=limit
        )

    @mcp.tool(
        name="modify_journal_entry",
        description=(
            "Update fields in an existing journal entry. "
            "ROB-568: supports US FX overrides (buy_fx_rate, sell_fx_rate, "
            "fx_rate_source, fx_pnl_accuracy). Updating closed US journals "
            "recomputes FX PnL."
        ),
    )
    async def modify_journal_entry_tool(
        journal_id: int,
        thesis: str | None = None,
        strategy: str | None = None,
        target_price: float | None = None,
        stop_loss: float | None = None,
        notes: str | None = None,
        buy_fx_rate: float | None = None,
        sell_fx_rate: float | None = None,
        fx_rate_source: str | None = None,
        fx_pnl_accuracy: str | None = None,
    ):
        return await modify_journal_entry(
            journal_id=journal_id,
            thesis=thesis,
            strategy=strategy,
            target_price=target_price,
            stop_loss=stop_loss,
            notes=notes,
            buy_fx_rate=buy_fx_rate,
            sell_fx_rate=sell_fx_rate,
            fx_rate_source=fx_rate_source,
            fx_pnl_accuracy=fx_pnl_accuracy,
        )

    _ = mcp.tool(
        name="save_trade_journal",
        description=(
            "Save a trade journal entry with investment thesis and strategy metadata. "
            "Call this when recommending a buy/sell to record WHY. "
            "symbol auto-detects instrument_type. min_hold_days sets hold_until. "
            "status defaults to 'draft' — set to 'active' after fill confirmation. "
            "account_type='paper'|'mock' for paper/mock journals (paper requires account name). "
            "paper_trade_id links to the paper trade record. "
            "paperclip_issue_id links to the Paperclip issue tracking this trade. "
            "metadata is an optional JSON dict for extensible fields."
        ),
    )(save_trade_journal)
    _ = mcp.tool(
        name="get_trade_journal",
        description=(
            "Query trade journals. MUST call before any sell recommendation to check "
            "thesis, hold period, target/stop prices. "
            "Returns active journals by default. "
            "Each entry includes hold_remaining_days and hold_expired. "
            "account_type defaults to None (all); set 'live'|'paper'|'mock' to filter. "
            "account (optional) filters to a specific account name. "
            "paperclip_issue_id (optional) reverse lookup by Paperclip issue ID. "
            "enrich_live (optional, default False): fetch live quotes to compute current_price/pnl_pct_live/target_reached/stop_reached and near_target/near_stop. Slower (one quote per returned entry); fail-open per entry."
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

# app/mcp_server/tooling/trade_journal_registration.py
"""MCP registration for trade journal tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.order_journal import (
    modify_journal_entry,
)
from app.mcp_server.tooling.trade_journal_tools import (
    get_trade_journal,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TRADE_JOURNAL_TOOL_NAMES: set[str] = {
    "get_trade_journal",
    "modify_journal_entry",
}


def register_trade_journal_tools(mcp: FastMCP) -> None:
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
        name="get_trade_journal",
        description=(
            "Query trade journals. MUST call before any sell recommendation to check "
            "thesis, hold period, target/stop prices. "
            "Returns active journals by default. "
            "Each entry includes hold_remaining_days and hold_expired. "
            "account_type defaults to None (all); set 'live'|'paper'|'mock' to filter. "
            "account (optional) filters to a specific account name. "
            "paperclip_issue_id (optional) reverse lookup by external issue key (legacy Paperclip name; current Linear ROB key). "
            "enrich_live (optional, default False): fetch live quotes to compute current_price/pnl_pct_live/target_reached/stop_reached and near_target/near_stop. Slower (one quote per returned entry); fail-open per entry."
        ),
    )(get_trade_journal)


__all__ = [
    "TRADE_JOURNAL_TOOL_NAMES",
    "register_trade_journal_tools",
]

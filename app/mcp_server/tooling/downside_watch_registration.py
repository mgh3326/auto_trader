# app/mcp_server/tooling/downside_watch_registration.py
"""MCP registration for the ROB-928 downside-watch auto-register sweep."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.services.downside_watch_service import DownsideWatchService

if TYPE_CHECKING:
    from fastmcp import FastMCP

DOWNSIDE_WATCH_TOOL_NAMES: set[str] = {"watch_downside_register_sweep"}

_SUPPORTED_MARKETS = frozenset({"kr"})


async def watch_downside_register_sweep_impl(
    market: str = "kr", dry_run: bool = True
) -> dict[str, Any]:
    """List (dry_run=True, default) or register (dry_run=False) support-break
    downside watches for currently held equity, mirroring
    ``review.trade_journals.stop_loss`` into ``review.investment_watch_alerts``.
    """
    if market not in _SUPPORTED_MARKETS:
        return {
            "success": False,
            "error": f"unsupported market: {market!r} (only 'kr' is implemented)",
        }
    async with AsyncSessionLocal() as db:
        result = await DownsideWatchService(db).register_sweep(dry_run=dry_run)
    return {"success": True, "market": market, **result}


def register_downside_watch_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="watch_downside_register_sweep",
        description=(
            "ROB-928 — sweep active KR equity holdings (review.trade_journals, "
            "status=active/account_type=live/side=buy) and register (or, with "
            "dry_run=True default, just list) support-break downside watch "
            "alerts: operator=below, intent=sell_review. Threshold = the "
            "journal stop_loss (max across lots for a symbol) when present, "
            "else the trailing 20-session KRX low. Skips a symbol that "
            "already has an active below+sell_review/risk_review watch "
            "(idempotent — safe to rerun). ROB-971 registration guard fetches "
            "the current price and skips an unavailable quote or a level already "
            "at/below threshold, preventing an immediate first-scan trigger. "
            "Notify-only registration "
            "(action_mode=notify_only): never submits, previews, or touches "
            "any order/broker path — the only mutation is inserting a "
            "review.investment_watch_alerts row via DirectWatchCreateService. "
            "Levels are NOT trailing stops; they are fixed at registration "
            "time, so rerun this sweep periodically to refresh them."
        ),
    )(watch_downside_register_sweep_impl)


__all__ = [
    "DOWNSIDE_WATCH_TOOL_NAMES",
    "register_downside_watch_tools",
    "watch_downside_register_sweep_impl",
]

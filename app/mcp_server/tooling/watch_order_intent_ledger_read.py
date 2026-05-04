"""Read-only watch order intent ledger MCP tools (ROB-103)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.review import WatchOrderIntentLedger
from app.routers.watch_order_intent_ledger import serialize_ledger_row

if TYPE_CHECKING:
    from fastmcp import FastMCP

WATCH_ORDER_INTENT_LEDGER_TOOL_NAMES: set[str] = {
    "watch_order_intent_ledger_list_recent",
    "watch_order_intent_ledger_get",
}


async def watch_order_intent_ledger_list_recent_impl(
    market: str | None = None,
    lifecycle_state: str | None = None,
    kst_date: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(int(limit), 100))
    async with AsyncSessionLocal() as db:
        stmt = select(WatchOrderIntentLedger).order_by(
            WatchOrderIntentLedger.created_at.desc()
        )
        if market is not None:
            stmt = stmt.where(WatchOrderIntentLedger.market == market.strip().lower())
        if lifecycle_state is not None:
            stmt = stmt.where(
                WatchOrderIntentLedger.lifecycle_state
                == lifecycle_state.strip().lower()
            )
        if kst_date is not None:
            stmt = stmt.where(WatchOrderIntentLedger.kst_date == kst_date.strip())
        stmt = stmt.limit(capped)
        rows = (await db.execute(stmt)).scalars().all()
        return {
            "success": True,
            "count": len(rows),
            "items": [serialize_ledger_row(r) for r in rows],
        }


async def watch_order_intent_ledger_get_impl(correlation_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(WatchOrderIntentLedger).where(
                    WatchOrderIntentLedger.correlation_id == correlation_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"success": False, "error": "not_found"}
        return {"success": True, "item": serialize_ledger_row(row)}


def register_watch_order_intent_ledger_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="watch_order_intent_ledger_list_recent",
        description=(
            "List recent watch_order_intent_ledger rows (read-only). "
            "Optional filters: market, lifecycle_state, kst_date. "
            "limit clamped to 1..100, default 20."
        ),
    )(watch_order_intent_ledger_list_recent_impl)
    mcp.tool(
        name="watch_order_intent_ledger_get",
        description="Fetch a single watch_order_intent_ledger row by correlation_id (read-only).",
    )(watch_order_intent_ledger_get_impl)


__all__ = [
    "WATCH_ORDER_INTENT_LEDGER_TOOL_NAMES",
    "register_watch_order_intent_ledger_tools",
    "watch_order_intent_ledger_get_impl",
    "watch_order_intent_ledger_list_recent_impl",
]

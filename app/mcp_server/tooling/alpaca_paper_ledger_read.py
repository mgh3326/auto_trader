"""Read-only MCP tools for the Alpaca Paper order ledger (ROB-84).

No broker mutation. No submit/cancel/replace. Pure record-keeping reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.alpaca_paper_anomaly_checks import (
    build_paper_execution_preflight_report,
)
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    d: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        elif hasattr(val, "__str__") and not isinstance(
            val, (str, int, float, bool, type(None), dict, list)
        ):
            val = str(val)
        d[col.name] = val
    return d


async def alpaca_paper_ledger_list_recent(
    limit: int = 50,
    lifecycle_state: str | None = None,
) -> dict[str, Any]:
    """List recent Alpaca Paper order ledger entries (read-only).

    Args:
        limit: Maximum number of rows to return (default 50, max 200).
        lifecycle_state: Optional filter — one of previewed, validation_failed,
            submitted, open, partially_filled, filled, canceled, unexpected.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(limit, 200)

    async with _session_factory()() as db:
        svc = AlpacaPaperLedgerService(db)
        rows = await svc.list_recent(limit=limit, lifecycle_state=lifecycle_state)

    items = [_row_to_dict(r) for r in rows]
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper_ledger",
        "lifecycle_state_filter": lifecycle_state,
        "limit": limit,
        "count": len(items),
        "items": items,
    }


async def alpaca_paper_ledger_get(
    client_order_id: str,
) -> dict[str, Any] | None:
    """Fetch one Alpaca Paper ledger entry by client_order_id (read-only).

    Returns None if not found.
    """
    if not client_order_id or not client_order_id.strip():
        raise ValueError("client_order_id is required")

    async with _session_factory()() as db:
        svc = AlpacaPaperLedgerService(db)
        row = await svc.get_by_client_order_id(client_order_id.strip())

    if row is None:
        return {
            "success": False,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper_ledger",
            "client_order_id": client_order_id,
            "found": False,
            "item": None,
        }
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper_ledger",
        "client_order_id": client_order_id,
        "found": True,
        "item": _row_to_dict(row),
    }


async def alpaca_paper_execution_preflight_check(
    limit: int = 50,
    open_orders: list[dict[str, Any]] | None = None,
    positions: list[dict[str, Any]] | None = None,
    approval_packet: dict[str, Any] | None = None,
    expected_signal_symbol: str | None = None,
    expected_execution_symbol: str | None = None,
    stale_after_minutes: int = 30,
) -> dict[str, Any]:
    """Run read-only Alpaca Paper execution anomaly checks.

    The tool reads recent ledger rows and combines them with optional caller-
    supplied read-only broker snapshots. It never submits, cancels, repairs, or
    writes data. The returned ``should_block`` field is intended for runner
    preflight gates.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(limit, 200)
    if stale_after_minutes < 1:
        raise ValueError("stale_after_minutes must be >= 1")

    async with _session_factory()() as db:
        svc = AlpacaPaperLedgerService(db)
        rows = await svc.list_recent(limit=limit)

    report = build_paper_execution_preflight_report(
        ledger_rows=rows,
        open_orders=open_orders or [],
        positions=positions or [],
        approval_packet=approval_packet,
        expected_signal_symbol=expected_signal_symbol,
        expected_execution_symbol=expected_execution_symbol,
        stale_after_minutes=stale_after_minutes,
    )
    data = report.to_dict()
    data.update(
        {
            "success": True,
            "account_mode": "alpaca_paper",
            "source": "alpaca_paper_execution_preflight",
            "read_only": True,
            "limit": limit,
        }
    )
    return data


def register_alpaca_paper_ledger_read_tools(mcp: FastMCP) -> None:
    """Register read-only Alpaca Paper ledger MCP tools."""
    _ = mcp.tool(
        name="alpaca_paper_ledger_list_recent",
        description=(
            "Read-only list of recent Alpaca Paper order ledger entries. "
            "Supports optional lifecycle_state filter and limit. No broker mutation."
        ),
    )(alpaca_paper_ledger_list_recent)
    _ = mcp.tool(
        name="alpaca_paper_ledger_get",
        description=(
            "Read-only fetch of one Alpaca Paper ledger entry by client_order_id. "
            "Returns found/not-found shape. No broker mutation."
        ),
    )(alpaca_paper_ledger_get)
    _ = mcp.tool(
        name="alpaca_paper_execution_preflight_check",
        description=(
            "Read-only Alpaca Paper execution anomaly preflight. Returns "
            "severity-classified findings and should_block for cycle runners. "
            "No broker mutation and no repair writes."
        ),
    )(alpaca_paper_execution_preflight_check)


__all__ = [
    "alpaca_paper_execution_preflight_check",
    "alpaca_paper_ledger_get",
    "alpaca_paper_ledger_list_recent",
    "register_alpaca_paper_ledger_read_tools",
]

"""Read-only MCP visibility for #728 long-term protection declarations.

This module intentionally reads only the durable declaration service.  It
does not call broker APIs, calculate send authority, or import the service
writer.  Fresh broker evidence is reserved for the authenticated operator
write route and live pre-send boundaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.services.protected_position_settings import read_protected_position_history
from app.services.protected_quantity_service import (
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


PROTECTED_POSITION_TOOL_NAMES: set[str] = {"get_protected_positions"}


def _head_payload(head: Any) -> dict[str, Any]:
    return {
        "account_scope": head.key.account_scope,
        "market": head.key.market,
        "symbol": head.key.symbol,
        "protected_quantity": format(head.protected_quantity, "f"),
        "revision": head.revision,
        "last_confirmed_broker_held": format(
            head.last_confirmed_broker_held,
            "f",
        ),
        "last_confirmed_at": head.last_confirmed_at.isoformat(),
        "updated_by_user_id": head.updated_by_user_id,
        "updated_at": head.updated_at.isoformat(),
    }


async def get_protected_positions_impl(
    *, account_scope: str | None = None, include_history: bool = False
) -> dict[str, Any]:
    """Read durable declaration heads and optionally their revision evidence."""

    try:
        async with AsyncSessionLocal() as session:
            service = ProtectedQuantityService(session)
            heads = await service.list(account_scope=account_scope)
            positions = [_head_payload(head) for head in heads]
            if include_history:
                for position, head in zip(positions, heads, strict=True):
                    position["history"] = await read_protected_position_history(
                        session,
                        key=head.key,
                    )
    except ProtectedQuantityValidationError as exc:
        return {
            "success": False,
            "error": "invalid_protection_scope",
            "message": str(exc),
        }
    except Exception:
        # Do not emit database connection details through a broad read tool.
        return {"success": False, "error": "protected_positions_unavailable"}
    return {
        "success": True,
        "account_scope": account_scope,
        "positions": positions,
    }


def register_protected_position_tools(mcp: FastMCP) -> None:
    """Register the one read-only declaration visibility tool."""

    @mcp.tool(
        name="get_protected_positions",
        description=(
            "Read durable long-term protected-position declarations and their "
            "revision metadata. This is read-only and does not query brokers, "
            "change declarations, or authorize an order."
        ),
    )
    async def get_protected_positions(
        account_scope: str | None = None,
        include_history: bool = False,
    ) -> dict[str, Any]:
        return await get_protected_positions_impl(
            account_scope=account_scope,
            include_history=include_history,
        )


__all__ = [
    "PROTECTED_POSITION_TOOL_NAMES",
    "get_protected_positions_impl",
    "register_protected_position_tools",
]

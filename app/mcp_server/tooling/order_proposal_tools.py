"""ROB-816 read/create MCP tools for order_proposals.

READ + CREATE ONLY. There is deliberately no approve/submit tool — approval is
Telegram-only (PR 2). ``order_proposal_create`` persists a proposal row; it
performs NO broker mutation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
)
from app.services.order_proposals.service import RungInput

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_PROPOSAL_TOOL_NAMES: set[str] = {
    "order_proposal_create",
    "order_proposal_get",
    "order_proposal_list",
}


def _dec(v: str | None) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal {v!r}") from exc


def _group_dict(g: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(g.proposal_id),
        "root_proposal_id": str(g.root_proposal_id),
        "revision": g.revision,
        "symbol": g.symbol,
        "market": g.market,
        "account_mode": g.account_mode,
        "side": g.side,
        "order_type": g.order_type,
        "proposer": g.proposer,
        "lifecycle_state": g.lifecycle_state,
        "thesis": g.thesis,
        "strategy": g.strategy,
        "supersedes_proposal_id": (
            str(g.supersedes_proposal_id) if g.supersedes_proposal_id else None
        ),
        "superseded_by_proposal_id": (
            str(g.superseded_by_proposal_id) if g.superseded_by_proposal_id else None
        ),
        "valid_until": g.valid_until.isoformat() if g.valid_until else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def _rung_dict(r: Any) -> dict[str, Any]:
    return {
        "rung_index": r.rung_index,
        "side": r.side,
        "quantity": str(r.quantity),
        "limit_price": str(r.limit_price) if r.limit_price is not None else None,
        "notional": str(r.notional) if r.notional is not None else None,
        "state": r.state,
        "broker_order_id": r.broker_order_id,
        "correlation_id": r.correlation_id,
    }


async def order_proposal_create(
    symbol: str,
    market: str,
    account_mode: str,
    side: str,
    order_type: str,
    proposer: str,
    rungs: list[dict],
    thesis: str | None = None,
    strategy: str | None = None,
    rationale: dict | None = None,
    broker_account_id: str | None = None,
    lot_context: dict | None = None,
    valid_until: str | None = None,
    supersedes_proposal_id: str | None = None,
) -> dict[str, Any]:
    """Create an order proposal (SOT ledger row). NOT a broker mutation — persists only.

    Args:
        rungs: list of {"rung_index": int, "side": str, "quantity": str,
               "limit_price": str|None, "notional": str|None}.
        supersedes_proposal_id: if this proposal replaces an existing one (price/qty
               change), the original is marked superseded and lineage is linked.
    """
    try:
        rung_inputs = [
            RungInput(
                int(r["rung_index"]),
                str(r["side"]),
                _dec(r["quantity"]),
                _dec(r.get("limit_price")),
                _dec(r.get("notional")),
            )
            for r in rungs
        ]
        vu = datetime.fromisoformat(valid_until) if valid_until else None
        sup = uuid.UUID(supersedes_proposal_id) if supersedes_proposal_id else None
        async with AsyncSessionLocal() as session:
            svc = OrderProposalsService(session)
            group = await svc.create_proposal(
                symbol=symbol,
                market=market,
                account_mode=account_mode,
                side=side,
                order_type=order_type,
                proposer=proposer,
                rungs=rung_inputs,
                thesis=thesis,
                strategy=strategy,
                rationale=rationale,
                broker_account_id=broker_account_id,
                lot_context=lot_context,
                valid_until=vu,
                supersedes_proposal_id=sup,
            )
            _, saved_rungs = await svc.get_proposal(group.proposal_id)
            await session.commit()
            return {
                "success": True,
                "proposal_id": str(group.proposal_id),
                "lifecycle_state": group.lifecycle_state,
                "rungs": [_rung_dict(r) for r in saved_rungs],
            }
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}


async def order_proposal_get(proposal_id: str) -> dict[str, Any]:
    """Fetch a proposal + its rungs (read-only)."""
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError:
        return {"success": False, "error": f"invalid proposal_id {proposal_id!r}"}
    async with AsyncSessionLocal() as session:
        svc = OrderProposalsService(session)
        try:
            group, rungs = await svc.get_proposal(pid)
        except OrderProposalNotFound:
            return {"success": False, "error": "not_found"}
        return {
            "success": True,
            "proposal": _group_dict(group),
            "rungs": [_rung_dict(r) for r in rungs],
        }


async def order_proposal_list(
    limit: int = 50,
    symbol: str | None = None,
    lifecycle_state: str | None = None,
) -> dict[str, Any]:
    """List recent proposals (read-only). limit is clamped to 1..200."""
    limit = max(1, min(int(limit), 200))
    async with AsyncSessionLocal() as session:
        svc = OrderProposalsService(session)
        rows = await svc.list_recent(
            limit=limit, symbol=symbol, lifecycle_state=lifecycle_state
        )
        return {
            "success": True,
            "count": len(rows),
            "proposals": [
                {**_group_dict(g), "rungs": [_rung_dict(r) for r in rs]}
                for g, rs in rows
            ],
        }


def register_order_proposal_tools(mcp: FastMCP) -> None:
    """Register the order_proposals read/create MCP tools.

    Deliberately excludes any approve/submit tool — approval is Telegram-only
    (ROB-816 PR 2).
    """
    _ = mcp.tool(
        name="order_proposal_create",
        description=(
            "Create an order proposal (SOT ledger row) with one or more rungs. "
            "NOT a broker mutation — persists only. Approval/submission happens "
            "via Telegram (PR 2), not through this tool."
        ),
    )(order_proposal_create)
    _ = mcp.tool(
        name="order_proposal_get",
        description="Read-only fetch of a proposal and its rungs by proposal_id.",
    )(order_proposal_get)
    _ = mcp.tool(
        name="order_proposal_list",
        description=(
            "Read-only list of recent order proposals, optionally filtered by "
            "symbol and/or lifecycle_state."
        ),
    )(order_proposal_list)


__all__ = [
    "ORDER_PROPOSAL_TOOL_NAMES",
    "order_proposal_create",
    "order_proposal_get",
    "order_proposal_list",
    "register_order_proposal_tools",
]

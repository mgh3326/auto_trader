"""Read-only MCP tools for the Alpaca Paper order ledger (ROB-84/ROB-90).

No broker mutation. No submit/cancel/replace. Pure record-keeping reads.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.alpaca_paper_anomaly_checks import (
    build_paper_execution_preflight_report,
)
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_roundtrip_report_service import (
    AlpacaPaperRoundtripReportService,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _clean_scope_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _packet_scope_value(packet: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(packet, dict):
        return None
    return _clean_scope_value(packet.get(key))


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
        lifecycle_state: Optional filter — one of the ROB-90 canonical states:
            planned, previewed, validated, submitted, filled,
            position_reconciled, sell_validated, closed, final_reconciled, anomaly.
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
    lifecycle_correlation_id: str | None = None,
    client_order_id: str | None = None,
    candidate_uuid: str | None = None,
    briefing_artifact_run_uuid: str | None = None,
    session_uuid: str | None = None,
) -> dict[str, Any]:
    """Run read-only Alpaca Paper execution anomaly checks.

    The tool reads recent or scope-specific ledger rows and combines them with
    optional caller-supplied read-only broker snapshots. It never submits,
    cancels, repairs, or writes data. The returned ``should_block`` field is
    intended for runner preflight gates.

    When a correlation/candidate/client/briefing scope is provided directly or
    via approval_packet, stale and symbol-context checks evaluate only rows in
    that scope. Calls without scope preserve the global recent-ledger safety
    behavior used by broad cycle runners. ``session_uuid`` is surfaced as a
    response scope marker for decision-session callers; ledger scoping still
    uses correlation/provenance keys because the Alpaca ledger has no session FK.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(limit, 200)
    if stale_after_minutes < 1:
        raise ValueError("stale_after_minutes must be >= 1")

    scope = {
        "lifecycle_correlation_id": _clean_scope_value(lifecycle_correlation_id)
        or _packet_scope_value(approval_packet, "lifecycle_correlation_id"),
        "client_order_id": _clean_scope_value(client_order_id)
        or _packet_scope_value(approval_packet, "client_order_id"),
        "candidate_uuid": _clean_scope_value(candidate_uuid)
        or _packet_scope_value(approval_packet, "candidate_uuid"),
        "briefing_artifact_run_uuid": _clean_scope_value(briefing_artifact_run_uuid)
        or _packet_scope_value(approval_packet, "briefing_artifact_run_uuid")
        or _packet_scope_value(approval_packet, "artifact_id"),
        "session_uuid": _clean_scope_value(session_uuid)
        or _packet_scope_value(approval_packet, "session_uuid")
        or _packet_scope_value(approval_packet, "decision_session_uuid"),
    }
    scoped_by: dict[str, str] | None = None

    async with _session_factory()() as db:
        svc = AlpacaPaperLedgerService(db)
        if scope["lifecycle_correlation_id"]:
            scoped_by = {
                "kind": "lifecycle_correlation_id",
                "value": scope["lifecycle_correlation_id"],
            }
            rows = await svc.list_by_correlation_id(scope["lifecycle_correlation_id"])
        elif scope["client_order_id"]:
            scoped_by = {"kind": "client_order_id", "value": scope["client_order_id"]}
            row = await svc.get_by_client_order_id(scope["client_order_id"])
            rows = [row] if row is not None else []
        elif scope["candidate_uuid"]:
            scoped_by = {"kind": "candidate_uuid", "value": scope["candidate_uuid"]}
            rows = await svc.list_by_candidate_uuid(uuid.UUID(scope["candidate_uuid"]))
        elif scope["briefing_artifact_run_uuid"]:
            scoped_by = {
                "kind": "briefing_artifact_run_uuid",
                "value": scope["briefing_artifact_run_uuid"],
            }
            rows = await svc.list_by_briefing_artifact_run_uuid(
                uuid.UUID(scope["briefing_artifact_run_uuid"])
            )
        else:
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
            "scoped_by": scoped_by,
            "session_uuid": scope["session_uuid"],
        }
    )
    return data


async def alpaca_paper_ledger_get_by_correlation(
    lifecycle_correlation_id: str,
) -> dict[str, Any]:
    """Fetch all Alpaca Paper ledger rows sharing a lifecycle_correlation_id (read-only).

    Returns the buy/sell roundtrip records in chronological order.
    lifecycle_correlation_id links buy and sell legs of the same paper roundtrip.
    """
    if not lifecycle_correlation_id or not lifecycle_correlation_id.strip():
        raise ValueError("lifecycle_correlation_id is required")

    async with _session_factory()() as db:
        svc = AlpacaPaperLedgerService(db)
        rows = await svc.list_by_correlation_id(lifecycle_correlation_id.strip())

    items = [_row_to_dict(r) for r in rows]
    return {
        "success": True,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper_ledger",
        "lifecycle_correlation_id": lifecycle_correlation_id,
        "count": len(items),
        "items": items,
    }


async def alpaca_paper_roundtrip_report(
    lifecycle_correlation_id: str | None = None,
    client_order_id: str | None = None,
    candidate_uuid: str | None = None,
    briefing_artifact_run_uuid: str | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    positions: list[dict[str, Any]] | None = None,
    stale_after_minutes: int = 30,
    include_ledger_rows: bool = True,
) -> dict[str, Any]:
    """Build a read-only Alpaca Paper roundtrip audit report.

    Exactly one of lifecycle_correlation_id, client_order_id, candidate_uuid, or
    briefing_artifact_run_uuid is required. open_orders and positions are
    optional caller-supplied read-only snapshots; this tool never fetches broker
    state itself.
    """
    supplied = [
        lifecycle_correlation_id is not None,
        client_order_id is not None,
        candidate_uuid is not None,
        briefing_artifact_run_uuid is not None,
    ]
    if sum(supplied) != 1:
        raise ValueError("exactly one lookup key is required")
    if stale_after_minutes < 1:
        raise ValueError("stale_after_minutes must be >= 1")

    candidate_lookup = uuid.UUID(candidate_uuid) if candidate_uuid is not None else None
    briefing_lookup = (
        uuid.UUID(briefing_artifact_run_uuid)
        if briefing_artifact_run_uuid is not None
        else None
    )

    async with _session_factory()() as db:
        svc = AlpacaPaperRoundtripReportService(db)
        if candidate_lookup is not None:
            response = await svc.build_reports_for_candidate_uuid(
                candidate_lookup,
                stale_after_minutes=stale_after_minutes,
                include_ledger_rows=include_ledger_rows,
            )
            payload = response.model_dump(mode="json")
            success = response.count > 0
        elif briefing_lookup is not None:
            response = await svc.build_reports_for_briefing_artifact_run_uuid(
                briefing_lookup,
                stale_after_minutes=stale_after_minutes,
                include_ledger_rows=include_ledger_rows,
            )
            payload = response.model_dump(mode="json")
            success = response.count > 0
        else:
            report = await svc.build_report(
                lifecycle_correlation_id=lifecycle_correlation_id,
                client_order_id=client_order_id,
                open_orders=open_orders or [],
                positions=positions or [],
                stale_after_minutes=stale_after_minutes,
                include_ledger_rows=include_ledger_rows,
            )
            payload = report.model_dump(mode="json")
            success = report.status != "not_found"

    return {
        "success": success,
        "account_mode": "alpaca_paper",
        "source": "alpaca_paper_roundtrip_report",
        "read_only": True,
        "report": payload,
    }


def register_alpaca_paper_ledger_read_tools(mcp: FastMCP) -> None:
    """Register read-only Alpaca Paper ledger MCP tools."""
    _ = mcp.tool(
        name="alpaca_paper_ledger_list_recent",
        description=(
            "Read-only list of recent Alpaca Paper order ledger entries. "
            "Supports optional lifecycle_state filter (ROB-90 canonical states: "
            "planned, previewed, validated, submitted, filled, position_reconciled, "
            "sell_validated, closed, final_reconciled, anomaly) and limit. "
            "No broker mutation."
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
            "Supports direct or approval_packet-derived correlation/client/"
            "candidate/briefing/session scope to avoid unrelated ledger rows. "
            "No broker mutation and no repair writes."
        ),
    )(alpaca_paper_execution_preflight_check)
    _ = mcp.tool(
        name="alpaca_paper_ledger_get_by_correlation",
        description=(
            "Read-only fetch of all Alpaca Paper ledger rows sharing a "
            "lifecycle_correlation_id. Returns the full buy/sell roundtrip records "
            "in chronological order. No broker mutation."
        ),
    )(alpaca_paper_ledger_get_by_correlation)
    _ = mcp.tool(
        name="alpaca_paper_roundtrip_report",
        description=(
            "Read-only Alpaca Paper roundtrip audit report from persisted ledger "
            "rows, with optional caller-supplied open_orders/positions snapshots. "
            "Does not call broker APIs and does not mutate database state."
        ),
    )(alpaca_paper_roundtrip_report)


__all__ = [
    "alpaca_paper_execution_preflight_check",
    "alpaca_paper_ledger_get",
    "alpaca_paper_ledger_get_by_correlation",
    "alpaca_paper_ledger_list_recent",
    "alpaca_paper_roundtrip_report",
    "register_alpaca_paper_ledger_read_tools",
]

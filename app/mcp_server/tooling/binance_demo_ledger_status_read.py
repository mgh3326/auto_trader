"""ROB-907 — Read-only MCP tool for the Binance Demo order ledger.

Operational point-in-time status of ``binance_demo_order_ledger`` (state
distribution, stuck-open roots, anomaly count, freshness) without a direct
production DB SELECT fallback. No broker call, no DB write, no secrets.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BLOCKING_ROOT_LIFECYCLE_STATES
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

if TYPE_CHECKING:
    from fastmcp import FastMCP

_MAX_RECENT_LIMIT = 200
_MAX_STALE_ROOTS = 50


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _isoformat(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
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


def _stale_root_summary(row: Any, *, as_of: dt.datetime) -> dict[str, Any]:
    planned_at = row.planned_at
    age_seconds = (
        (as_of - planned_at).total_seconds() if planned_at is not None else None
    )
    return {
        "client_order_id": row.client_order_id,
        "product": row.product,
        "instrument_id": row.instrument_id,
        "lifecycle_state": row.lifecycle_state,
        "planned_at": _isoformat(planned_at),
        "age_seconds": age_seconds,
    }


async def binance_demo_ledger_status(
    stale_age_seconds: int = 3600,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """Read-only status summary of the Binance Demo order ledger.

    Args:
        stale_age_seconds: An open root lifecycle (planned/previewed/validated/
            submitted/filled/anomaly, ``parent_client_order_id IS NULL``) older
            than this many seconds is surfaced in ``stale_open_roots``.
        recent_limit: Number of most-recently-updated rows to include in
            ``recent`` (default 20, max 200).

    Returns a dict with ``status_distribution`` (lifecycle_state -> count,
    table-wide across products), ``open_root_count`` (blocking root
    lifecycles — the same definition the executor's capacity gate uses),
    ``anomaly_count``, ``stale_open_roots`` (bounded, oldest first),
    ``latest_activity_at``, ``recent`` (bounded), and ``as_of``.

    No broker call. No database write.
    """
    if stale_age_seconds < 0:
        raise ValueError("stale_age_seconds must be >= 0")
    if recent_limit < 1:
        raise ValueError("recent_limit must be >= 1")
    recent_limit = min(recent_limit, _MAX_RECENT_LIMIT)

    as_of = dt.datetime.now(dt.UTC)
    older_than = as_of - dt.timedelta(seconds=stale_age_seconds)

    async with _session_factory()() as db:
        svc = BinanceDemoLedgerService(db)
        status_distribution = await svc.status_distribution()
        open_root_count = await svc.count_open_lifecycles()
        stale_roots = await svc.stale_open_roots(
            older_than=older_than, limit=_MAX_STALE_ROOTS
        )
        latest_activity_at = await svc.latest_activity_at()
        recent = await svc.list_recent(limit=recent_limit)

    anomaly_count = sum(
        count for state, count in status_distribution.items() if state == "anomaly"
    )

    return {
        "success": True,
        "source": "binance_demo_order_ledger",
        "read_only": True,
        "status_distribution": status_distribution,
        "open_root_states": list(BLOCKING_ROOT_LIFECYCLE_STATES),
        "open_root_count": open_root_count,
        "anomaly_count": anomaly_count,
        "stale_age_seconds": stale_age_seconds,
        "stale_open_roots": [
            _stale_root_summary(row, as_of=as_of) for row in stale_roots
        ],
        "latest_activity_at": _isoformat(latest_activity_at),
        "recent_limit": recent_limit,
        "recent": [_row_to_dict(row) for row in recent],
        "as_of": as_of.isoformat(),
    }


def register_binance_demo_ledger_status_tool(mcp: FastMCP) -> None:
    """Register the read-only Binance Demo ledger status MCP tool."""
    _ = mcp.tool(
        name="binance_demo_ledger_status",
        description=(
            "Read-only Binance Demo order ledger status: lifecycle_state "
            "distribution (table-wide), open root lifecycle count, anomaly "
            "count, stale (stuck-open) root lifecycles older than "
            "stale_age_seconds, latest activity timestamp, and a bounded "
            "recent-activity list. No broker call, no database write."
        ),
    )(binance_demo_ledger_status)


__all__ = [
    "binance_demo_ledger_status",
    "register_binance_demo_ledger_status_tool",
]

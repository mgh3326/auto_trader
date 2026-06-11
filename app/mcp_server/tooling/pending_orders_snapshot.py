"""Shared read-only pending order snapshot collection for MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",
    "crypto": "upbit_live",
}


@dataclass(frozen=True)
class PendingOrdersSnapshot:
    orders: list[dict[str, Any]] | None
    as_of: str | None
    freshness_status: str | None
    unavailable_reason: str | None
    account_scope: str | None


async def collect_pending_orders_snapshot(
    db: Any,
    *,
    market: str,
    account_scope: str | None,
) -> PendingOrdersSnapshot:
    from app.services.action_report.snapshot_backed.collectors.registry import (
        production_collector_registry,
    )
    from app.services.investment_snapshots.collectors import CollectorRequest

    effective_scope = account_scope or DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if effective_scope is None:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="unsupported_market",
            account_scope=None,
        )

    try:
        registry = production_collector_registry(db)
    except Exception as exc:  # noqa: BLE001
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason=f"collector_registry_failed:{type(exc).__name__}:{exc}",
            account_scope=effective_scope,
        )
    collector = registry.get("pending_orders")
    if collector is None:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="collector_missing",
            account_scope=effective_scope,
        )

    try:
        results = await collector.collect(
            CollectorRequest(
                market=market,  # type: ignore[arg-type]
                account_scope=effective_scope,  # type: ignore[arg-type]
                policy_snapshot={},
            )
        )
    except Exception as exc:  # noqa: BLE001
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason=f"collector_failed:{type(exc).__name__}:{exc}",
            account_scope=effective_scope,
        )
    if not results:
        return PendingOrdersSnapshot(
            orders=None,
            as_of=None,
            freshness_status=None,
            unavailable_reason="collector_returned_no_results",
            account_scope=effective_scope,
        )

    result = results[0]
    as_of = result.as_of.isoformat() if result.as_of is not None else None
    freshness = result.freshness_status
    errors = result.errors_json or {}
    if freshness in ("unavailable", "hard_stale"):
        return PendingOrdersSnapshot(
            orders=None,
            as_of=as_of,
            freshness_status=freshness,
            unavailable_reason=str(errors.get("reason") or freshness),
            account_scope=effective_scope,
        )
    payload = result.payload_json or {}
    orders = payload.get("pending_orders")
    return PendingOrdersSnapshot(
        orders=list(orders) if orders is not None else [],
        as_of=as_of,
        freshness_status=freshness,
        unavailable_reason=None,
        account_scope=effective_scope,
    )

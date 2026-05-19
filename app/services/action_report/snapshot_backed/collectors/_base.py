"""Base helpers shared by all snapshot-backed report collectors."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.action_report.common.jsonable import to_jsonable
from app.services.action_report.common.source_kind_mapping import map_source_kind
from app.services.investment_snapshots.collectors import SnapshotCollectResult


def utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def build_result(
    *,
    snapshot_kind: str,
    market: str,
    account_scope: str | None,
    payload: dict[str, Any],
    origin: str,
    as_of: dt.datetime,
    freshness_status: str = "fresh",
    symbol: str | None = None,
    coverage: dict[str, Any] | None = None,
    errors: dict[str, Any] | None = None,
) -> SnapshotCollectResult:
    """Build a :class:`SnapshotCollectResult` with JSON-safe payload + mapped source_kind."""
    return SnapshotCollectResult(
        snapshot_kind=snapshot_kind,
        market=market,
        account_scope=account_scope,
        symbol=symbol,
        source_kind=map_source_kind(origin),
        payload_json=to_jsonable(payload),
        coverage_json=to_jsonable(coverage or {}),
        errors_json=to_jsonable(errors or {}),
        as_of=as_of,
        freshness_status=freshness_status,  # type: ignore[arg-type]
    )


def unavailable_result(
    *,
    snapshot_kind: str,
    market: str,
    account_scope: str | None,
    origin: str,
    reason: str,
    as_of: dt.datetime,
) -> SnapshotCollectResult:
    """Build an explicit ``unavailable`` result. Marks the kind attempted."""
    return SnapshotCollectResult(
        snapshot_kind=snapshot_kind,
        market=market,
        account_scope=account_scope,
        source_kind=map_source_kind(origin),
        payload_json={},
        errors_json=to_jsonable({"reason": reason}),
        as_of=as_of,
        freshness_status="unavailable",
    )

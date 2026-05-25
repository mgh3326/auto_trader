"""ROB-269 Phase 2 — MCP tools for the snapshot foundation.

Four tools:
* ``investment_snapshot_bundle_ensure`` — reuse or create a bundle.
* ``investment_snapshot_bundle_get`` — fetch one bundle + items.
* ``investment_snapshot_list`` — filtered list of snapshot metadata.
* ``investment_snapshot_refresh_request`` — log a refresh ask (no collection).

Read-only with respect to broker/order state. The only writes are
append-only INSERTs into ``review.investment_snapshot_*``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.schemas.investment_snapshots_mcp import (
    EnsureBundleRequest,
    ListBundlesRequest,
    ListSnapshotsRequest,
    RefreshRequest,
)
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.read_service import (
    SnapshotBundleNotFoundError,
    SnapshotBundleReadService,
)
from app.services.investment_snapshots.refresh_request_service import (
    SnapshotRefreshRequestService,
)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


# ---------------------------------------------------------------------------
# investment_snapshot_bundle_ensure
# ---------------------------------------------------------------------------
async def investment_snapshot_bundle_ensure(
    purpose: str,
    market: str,
    policy_version: str = "intraday_action_report_v1",
    account_scope: str | None = None,
    mode: str = "ensure_fresh",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Ensure a snapshot bundle exists for the given identity tuple.

    Phase 2 note: the production collector registry is empty, so callers without
    pre-collected data will most often get back ``status='reused'`` (when a
    fresh bundle exists) or ``status='failed'`` (when none does). Use
    ``investment_snapshot_refresh_request`` to ask the Phase 3 scheduler to
    refresh.
    """
    request = EnsureBundleRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        policy_version=policy_version,
        mode=mode,  # type: ignore[arg-type]
        symbols=symbols,
        candidate_limit=candidate_limit,
        requested_by=requested_by,  # type: ignore[arg-type]
    )
    async with _session_factory()() as db:
        # ROB-314: deliberately NOT wired to production_collector_registry.
        # This is the generic bundle-ensure primitive — callers feed manual
        # snapshots or rely on reuse. Production collectors are injected only
        # at the report-generation entrypoints. Locked by
        # tests/test_rob314_deferred_call_sites.py.
        svc = SnapshotBundleEnsureService(db)
        response = await svc.ensure(request)
        await db.commit()
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# investment_snapshot_bundle_get
# ---------------------------------------------------------------------------
async def investment_snapshot_bundle_get(
    bundle_uuid: str,
    include_payload_preview: bool = False,
) -> dict[str, Any]:
    """Fetch one bundle by UUID, with linked items.

    Returns ``{'success': False, 'error': 'not_found'}`` when the UUID does
    not resolve. ``include_payload_preview=True`` adds at most 2KB of
    serialised JSON per item under ``payload_previews`` (keyed by
    ``snapshot_uuid``).
    """
    try:
        parsed = uuid.UUID(bundle_uuid)
    except ValueError:
        return {"success": False, "error": "invalid_uuid", "bundle_uuid": bundle_uuid}

    async with _session_factory()() as db:
        svc = SnapshotBundleReadService(db)
        try:
            response = await svc.get_bundle(
                bundle_uuid=parsed,
                include_payload_preview=include_payload_preview,
            )
        except SnapshotBundleNotFoundError:
            return {
                "success": False,
                "error": "not_found",
                "bundle_uuid": bundle_uuid,
            }
    return {"success": True, **response.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# investment_snapshot_list
# ---------------------------------------------------------------------------
async def investment_snapshot_list(
    market: str | None = None,
    symbol: str | None = None,
    snapshot_kind: str | None = None,
    source_kind: str | None = None,
    freshness_status: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent snapshot metadata (no payload bodies returned).

    Filters are all optional; ``limit`` is clamped to ``[1, 100]``.
    ``since`` is ISO-8601 with timezone (e.g. ``2026-05-19T11:00:00+09:00``).
    """
    parsed_since = None
    if since is not None:
        try:
            from datetime import datetime

            parsed_since = datetime.fromisoformat(since)
        except ValueError:
            return {"success": False, "error": "invalid_since", "since": since}

    request = ListSnapshotsRequest(
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        snapshot_kind=snapshot_kind,  # type: ignore[arg-type]
        source_kind=source_kind,  # type: ignore[arg-type]
        freshness_status=freshness_status,  # type: ignore[arg-type]
        since=parsed_since,
        limit=max(1, min(limit, 100)),
    )
    async with _session_factory()() as db:
        svc = SnapshotBundleReadService(db)
        response = await svc.list_snapshots(request)
    return {"success": True, **response.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# investment_snapshot_refresh_request
# ---------------------------------------------------------------------------
async def investment_snapshot_refresh_request(
    reason: str,
    market: str,
    purpose: str = "manual_refresh",
    account_scope: str | None = None,
    symbols: list[str] | None = None,
    snapshot_kinds: list[str] | None = None,
    policy_version: str = "intraday_action_report_v1",
    requested_by: str = "user",
) -> dict[str, Any]:
    """Record a refresh request (inserts one run row; no collection in Phase 2).

    The Phase 3 scheduler picks up runs with purpose='manual_refresh' /
    'reviewer_requested' and acts on them.
    """
    request = RefreshRequest(
        reason=reason,
        purpose=purpose,  # type: ignore[arg-type]
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=symbols,
        snapshot_kinds=snapshot_kinds,  # type: ignore[arg-type]
        policy_version=policy_version,
        requested_by=requested_by,  # type: ignore[arg-type]
    )
    async with _session_factory()() as db:
        svc = SnapshotRefreshRequestService(db)
        response = await svc.record(request)
        await db.commit()
    return {"success": True, **response.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Bundles list (HTTP-only mirror; exposed as MCP tool too for completeness)
# ---------------------------------------------------------------------------
async def investment_snapshot_bundle_list(
    purpose: str | None = None,
    market: str | None = None,
    account_scope: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent bundles (header-only). Filters all optional."""
    request = ListBundlesRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        limit=max(1, min(limit, 100)),
    )
    async with _session_factory()() as db:
        svc = SnapshotBundleReadService(db)
        response = await svc.list_bundles(request)
    return {"success": True, **response.model_dump(mode="json")}

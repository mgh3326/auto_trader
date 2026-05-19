"""ROB-269 Phase 2 — Snapshot bundles / snapshots GET endpoints.

GET-only. No POST/PUT/DELETE — refresh-request lives at the MCP surface
only in Phase 2, keeping the HTTP surface provably read-only. Mounted
into the FastAPI app only when ``settings.INVESTMENT_SNAPSHOTS_MCP_ENABLED``
is True (see ``app.main``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.investment_snapshots_mcp import (
    GetBundleResponse,
    ListBundlesRequest,
    ListBundlesResponse,
    ListSnapshotsRequest,
    ListSnapshotsResponse,
)
from app.services.investment_snapshots.read_service import (
    SnapshotBundleNotFoundError,
    SnapshotBundleReadService,
)

router = APIRouter(prefix="/trading", tags=["investment-snapshots"])


@router.get(
    "/api/investment-snapshots/bundles/{bundle_uuid}",
    response_model=GetBundleResponse,
)
async def get_investment_snapshot_bundle(
    bundle_uuid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include_payload_preview: bool = Query(False),
) -> GetBundleResponse:
    _ = current_user  # auth-only dependency
    svc = SnapshotBundleReadService(db)
    try:
        return await svc.get_bundle(
            bundle_uuid=bundle_uuid,
            include_payload_preview=include_payload_preview,
        )
    except SnapshotBundleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"bundle not found: {bundle_uuid}",
        ) from exc


@router.get(
    "/api/investment-snapshots/bundles",
    response_model=ListBundlesResponse,
)
async def list_investment_snapshot_bundles(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    purpose: str | None = Query(None),
    market: str | None = Query(None),
    account_scope: str | None = Query(None),
    bundle_status: str | None = Query(None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListBundlesResponse:
    _ = current_user
    svc = SnapshotBundleReadService(db)
    request = ListBundlesRequest(
        purpose=purpose,
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        status=bundle_status,  # type: ignore[arg-type]
        limit=limit,
    )
    return await svc.list_bundles(request)


@router.get(
    "/api/investment-snapshots/snapshots",
    response_model=ListSnapshotsResponse,
)
async def list_investment_snapshots(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: str | None = Query(None),
    symbol: str | None = Query(None),
    snapshot_kind: str | None = Query(None),
    source_kind: str | None = Query(None),
    freshness_status: str | None = Query(None),
    since: dt.datetime | None = Query(None),
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListSnapshotsResponse:
    _ = current_user
    svc = SnapshotBundleReadService(db)
    request = ListSnapshotsRequest(
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        snapshot_kind=snapshot_kind,  # type: ignore[arg-type]
        source_kind=source_kind,  # type: ignore[arg-type]
        freshness_status=freshness_status,  # type: ignore[arg-type]
        since=since,
        limit=limit,
    )
    return await svc.list_snapshots(request)

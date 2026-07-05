"""FastAPI router for /invest analysis-artifact read surface (ROB-664).

Read-only exposure of ``review.analysis_artifacts`` (ROB-637/648): list with
market/kind/readiness_label/symbol filters + is_stale badge, and per-artifact
detail with payload. Writes (analysis_artifact_save) stay MCP-only; no
broker/order/watch mutation is reachable from here. List responses omit the
payload (ROB-504 lesson) — payload loads only on the detail endpoint.
"""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.analysis_artifact import (
    AnalysisArtifactGetResponse,
    AnalysisArtifactKindLiteral,
    AnalysisArtifactListRequest,
    AnalysisArtifactListResponse,
    AnalysisArtifactMeta,
    AnalysisArtifactRead,
    AnalysisArtifactReadinessLiteral,
)
from app.schemas.investment_reports import MarketLiteral
from app.services.analysis_artifact import AnalysisArtifactService

router = APIRouter(
    prefix="/trading/api/invest/artifacts",
    tags=["invest-artifacts"],
)

_VALID_MARKETS = frozenset(get_args(MarketLiteral))
_VALID_KINDS = frozenset(get_args(AnalysisArtifactKindLiteral))
_VALID_READINESS = frozenset(get_args(AnalysisArtifactReadinessLiteral))


def _validate(name: str, value: str | None, allowed: frozenset[str]) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=422, detail=f"invalid {name}: {value}")


@router.get("/")
async def list_artifacts(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    readiness_label: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    include_stale: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    correlation_id: Annotated[list[str] | None, Query()] = None,
) -> AnalysisArtifactListResponse:
    _validate("market", market, _VALID_MARKETS)
    _validate("kind", kind, _VALID_KINDS)
    _validate("readiness_label", readiness_label, _VALID_READINESS)
    svc = AnalysisArtifactService(db)
    rows = await svc.list_artifacts(
        market=market,
        kind=kind,
        readiness_label=readiness_label,
        symbol=symbol,
        include_stale=include_stale,
        limit=limit,
        correlation_ids=correlation_id,
    )
    filters = AnalysisArtifactListRequest(
        market=market,
        kind=kind,
        readiness_label=readiness_label,
        symbol=symbol,
        include_stale=include_stale,
        limit=limit,
        correlation_id=",".join(correlation_id) if correlation_id else None,
    )
    metas = [AnalysisArtifactMeta.model_validate(r) for r in rows]
    return AnalysisArtifactListResponse(
        count=len(metas), filters=filters, artifacts=metas
    )


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnalysisArtifactGetResponse:
    svc = AnalysisArtifactService(db)
    row = await svc.get(artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return AnalysisArtifactGetResponse(
        artifact=AnalysisArtifactRead.model_validate(row)
    )

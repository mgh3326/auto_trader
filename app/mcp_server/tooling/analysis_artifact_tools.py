"""MCP tools for ROB-637 analysis artifact persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.schemas.analysis_artifact import (
    AnalysisArtifactGetResponse,
    AnalysisArtifactListRequest,
    AnalysisArtifactListResponse,
    AnalysisArtifactRead,
    AnalysisArtifactSave,
    AnalysisArtifactSaveResponse,
)
from app.services.analysis_artifact import AnalysisArtifactService


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    return {
        "success": False,
        "error": "invalid_request",
        "detail": exc.errors(),
    }


async def analysis_artifact_save(
    market: str,
    kind: str,
    title: str,
    symbols: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    as_of: str | None = None,
    valid_until: str | None = None,
    created_by: str = "claude",
    session_label: str | None = None,
) -> dict[str, Any]:
    """Persist a single analysis artifact for cross-session reuse."""
    try:
        entry = AnalysisArtifactSave.model_validate(
            {
                "market": market,
                "kind": kind,
                "title": title,
                "symbols": symbols or [],
                "payload": payload or {},
                "as_of": as_of or datetime.now(tz=UTC).isoformat(),
                "valid_until": valid_until,
                "created_by": created_by,
                "session_label": session_label,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = AnalysisArtifactService(db)
        row = await service.save(entry)
        await db.commit()
        response = AnalysisArtifactSaveResponse(
            artifact=AnalysisArtifactRead.model_validate(row),
        )
    return response.model_dump(mode="json")


async def analysis_artifact_list(
    market: str | None = None,
    kind: str | None = None,
    symbol: str | None = None,
    since: str | None = None,
    include_stale: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Return analysis artifacts matching filters, newest ``as_of`` first."""
    try:
        request = AnalysisArtifactListRequest.model_validate(
            {
                "market": market,
                "kind": kind,
                "symbol": symbol,
                "since": since,
                "include_stale": include_stale,
                "limit": limit,
            }
        )
    except ValidationError as exc:
        return _validation_error(exc)

    async with AsyncSessionLocal() as db:
        service = AnalysisArtifactService(db)
        rows = await service.list_artifacts(
            market=request.market,
            kind=request.kind,
            symbol=request.symbol,
            since=request.since,
            include_stale=request.include_stale,
            limit=request.limit,
        )
        response = AnalysisArtifactListResponse(
            count=len(rows),
            filters=request,
            artifacts=[
                AnalysisArtifactRead.model_validate(row) for row in rows
            ],
        )
    return response.model_dump(mode="json")


async def analysis_artifact_get(
    artifact_id: int | str,
) -> dict[str, Any]:
    """Return a single analysis artifact by id or artifact_uuid."""
    async with AsyncSessionLocal() as db:
        service = AnalysisArtifactService(db)
        row = await service.get(artifact_id)
        if row is None:
            return {
                "success": False,
                "error": "not_found",
                "artifact_id": artifact_id,
            }
        response = AnalysisArtifactGetResponse(
            artifact=AnalysisArtifactRead.model_validate(row),
        )
    return response.model_dump(mode="json")

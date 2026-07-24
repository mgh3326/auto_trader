"""MCP tools for ROB-637 analysis artifact persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.schemas.analysis_artifact import (
    AnalysisArtifactGetResponse,
    AnalysisArtifactListRequest,
    AnalysisArtifactListResponse,
    AnalysisArtifactMeta,
    AnalysisArtifactRead,
    AnalysisArtifactSave,
    AnalysisArtifactSaveResponse,
)
from app.services.analysis_artifact import AnalysisArtifactService

# Save-side payload cap (ROB-628 size discipline). Measured on real UTF-8
# bytes (ensure_ascii=False) so Korean payloads are not ~6x over-counted.
PAYLOAD_CAP_BYTES = 100 * 1024


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
    correlation_id: str | None = None,
    account_scope: str | None = None,
    readiness_label: str | None = None,
) -> dict[str, Any]:
    """Persist a single analysis artifact for cross-session reuse."""
    size_bytes = len(
        json.dumps(payload or {}, ensure_ascii=False, default=str).encode("utf-8")
    )
    if size_bytes > PAYLOAD_CAP_BYTES:
        return {
            "success": False,
            "error": "payload_too_large",
            "size_bytes": size_bytes,
            "cap_bytes": PAYLOAD_CAP_BYTES,
        }
    async with AsyncSessionLocal() as db:
        service = AnalysisArtifactService(db)
        resolved_as_of = as_of
        if resolved_as_of is None and correlation_id is not None:
            existing = await service.get_by_correlation_id(correlation_id)
            if existing is not None:
                # An omitted timestamp on an idempotent retry means "reuse the
                # stored evidence time", never "renew to request now".
                resolved_as_of = existing.as_of.isoformat()
        try:
            entry = AnalysisArtifactSave.model_validate(
                {
                    "market": market,
                    "kind": kind,
                    "title": title,
                    "symbols": symbols or [],
                    "payload": payload or {},
                    "as_of": resolved_as_of or datetime.now(tz=UTC).isoformat(),
                    "valid_until": valid_until,
                    "created_by": created_by,
                    "session_label": session_label,
                    "correlation_id": correlation_id,
                    "account_scope": account_scope,
                    "readiness_label": readiness_label,
                }
            )
        except ValidationError as exc:
            return _validation_error(exc)
        row, action = await service.save(entry)
        await db.commit()
        response = AnalysisArtifactSaveResponse(
            action=action,  # type: ignore[arg-type]
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
    correlation_id: str | None = None,
    account_scope: str | None = None,
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
                "correlation_id": correlation_id,
                "account_scope": account_scope,
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
            correlation_id=request.correlation_id,
            account_scope=request.account_scope,
        )
        response = AnalysisArtifactListResponse(
            count=len(rows),
            filters=request,
            # Metadata-only on purpose (token-lean): payload is served by
            # analysis_artifact_get, never by list.
            artifacts=[AnalysisArtifactMeta.model_validate(row) for row in rows],
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

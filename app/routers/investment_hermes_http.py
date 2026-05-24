"""ROB-287 — HTTP transport for the four Hermes MCP tools.

Mirrors the MCP surface in ``app/mcp_server/tooling/investment_hermes_handlers.py``
1:1 so Hermes can pick the transport (MCP or HTTP) freely without
behaviour drift. Auth is delegated to :class:`AuthMiddleware`, which
gates every path under
``/trading/api/investment-reports/hermes/`` on the ``HERMES_INGEST_TOKEN``
shared secret (header name configurable via
``HERMES_INGEST_TOKEN_HEADER``).

Body schemas, validation, and business-logic outcomes route through
the exact same service classes the MCP tools use, so the contract
acceptance tests on the service layer cover both transports.

Hard invariants:

* Auth-gated by ``HERMES_INGEST_TOKEN`` at the middleware layer; an
  unconfigured token responds ``403 "token not configured"``.
* When ``settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED`` is off,
  every endpoint short-circuits with ``503`` and a structured body —
  same code path the MCP gate-off uses.
* No in-process LLM is invoked.
* No broker / order / watch / order-intent mutation reachable from
  these endpoints.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.schemas.hermes_composition import (
    HermesCompositionIngestRequest,
    HermesCompositionResult,
    HermesStageArtifactsIngestRequest,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.schemas.investment_symbol_reports import HermesSymbolReportsIngestRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_stages.hermes_context import (
    HermesContextExporter,
    HermesContextExportError,
)
from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestError,
    HermesCompositionIngestService,
    HermesStageArtifactsIngestError,
    HermesStageArtifactsIngestService,
)
from app.schemas.investment_dimension_reports import HermesDimensionReportsIngestRequest
from app.services.investment_dimensions.dimension_report_ingest import (
    DimensionReportIngestService,
    DimensionReportIngestError,
)
from app.services.investment_stages.symbol_report_ingest import (
    SymbolIntermediateReportIngestService,
    SymbolReportIngestError,
)

router = APIRouter(
    prefix="/trading/api/investment-reports/hermes",
    tags=["investment-reports-hermes"],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _gate_off_503() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "snapshot_backed_report_generator_disabled",
            "hint": (
                "Set SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true on the "
                "host to enable the Hermes HTTP ingest endpoints."
            ),
        },
    )


def _require_enabled() -> None:
    if not settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED:
        raise _gate_off_503()


# ---------------------------------------------------------------------------
# Request body schemas (HTTP layer)
#
# Kept distinct from the underlying ``EnsureBundleRequest`` etc. so the
# HTTP wire shape can evolve without forcing the service-level schemas
# to follow.
# ---------------------------------------------------------------------------


class _PrepareBundleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    account_scope: str | None = None
    policy_version: str = "intraday_action_report_v1"
    mode: str = "ensure_fresh"
    symbols: list[str] | None = None
    candidate_limit: int | None = None
    requested_by: str = "hermes"
    purpose: str = "report_generation"


class _GetHermesContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_bundle_uuid: uuid.UUID = Field(
        description="UUID of the persisted snapshot bundle to export context for."
    )


class _CreateFromCompositionBody(BaseModel):
    """Top-level body for the composition ingest endpoint.

    Pulled into its own body model rather than reusing
    ``HermesCompositionIngestRequest`` directly so we can keep the HTTP
    envelope's field set explicit (``status`` literal handled here, then
    passed through verbatim to the service).
    """

    model_config = ConfigDict(extra="forbid")

    composition: HermesCompositionResult
    kst_date: str
    market: str
    market_session: str | None = None
    account_scope: str | None = None
    report_type: str = "snapshot_backed_advisory_v1"
    generator_version: str = "hermes-composition.v1"
    policy_version: str = "intraday_action_report_v1"
    status: str = "draft"
    created_by_profile: str = "HERMES_ADVISOR"


# ---------------------------------------------------------------------------
# POST /prepare-bundle
# ---------------------------------------------------------------------------


@router.post("/prepare-bundle")
async def prepare_bundle(
    body: _PrepareBundleBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    _require_enabled()

    try:
        ensure_request = EnsureBundleRequest(
            purpose=body.purpose,
            market=body.market,  # type: ignore[arg-type]
            account_scope=body.account_scope,  # type: ignore[arg-type]
            policy_version=body.policy_version,
            mode=body.mode,  # type: ignore[arg-type]
            symbols=body.symbols,
            candidate_limit=body.candidate_limit,
            requested_by=body.requested_by,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_prepare_bundle_request",
                "validation": exc.errors(),
            },
        ) from exc

    svc = SnapshotBundleEnsureService(db)
    response = await svc.ensure(ensure_request)
    await db.commit()
    return {"success": True, **response.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# POST /context
# ---------------------------------------------------------------------------


@router.post("/context")
async def get_hermes_context(
    body: _GetHermesContextBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    _require_enabled()

    exporter = HermesContextExporter(db)
    try:
        payload = await exporter.export(snapshot_bundle_uuid=body.snapshot_bundle_uuid)
    except HermesContextExportError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": str(body.snapshot_bundle_uuid),
                "message": str(exc),
            },
        ) from exc
    return {"success": True, **payload.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# POST /stage-artifacts
# ---------------------------------------------------------------------------


_INGEST_ERROR_HTTP_STATUS: dict[str, int] = {
    "snapshot_bundle_not_found": status.HTTP_404_NOT_FOUND,
    "run_envelope_mismatch": status.HTTP_409_CONFLICT,
    "artifact_content_conflict": status.HTTP_409_CONFLICT,
    "append_only_race": status.HTTP_409_CONFLICT,
    # ROB-301 symbol-reports ingest codes.
    "stage_run_not_found": status.HTTP_404_NOT_FOUND,
    "symbol_report_race": status.HTTP_409_CONFLICT,
    # open_action_missing_side / unknown_bucket fall through to 400.
}


@router.post("/stage-artifacts")
async def stage_artifacts_ingest(
    body: HermesStageArtifactsIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    _require_enabled()

    svc = HermesStageArtifactsIngestService(db)
    try:
        response = await svc.ingest_stage_artifacts(body)
    except HermesStageArtifactsIngestError as exc:
        await db.rollback()
        http_status = _INGEST_ERROR_HTTP_STATUS.get(
            exc.code, status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=http_status,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return {
        "success": True,
        "run_uuid": str(response.run.run_uuid),
        "run_status": response.run.status,
        "snapshot_bundle_uuid": str(response.run.snapshot_bundle_uuid),
        "artifacts": [
            {
                "stage_type": r.stage_type,
                "artifact_uuid": str(r.artifact.artifact_uuid),
                "idempotent_existing": r.idempotent_existing,
            }
            for r in response.results
        ],
    }


# ---------------------------------------------------------------------------
# POST /composition
# ---------------------------------------------------------------------------


@router.post("/composition")
async def composition_ingest(
    body: _CreateFromCompositionBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    _require_enabled()

    try:
        ingest_request = HermesCompositionIngestRequest(
            composition=body.composition,
            kst_date=body.kst_date,
            market=body.market,
            market_session=body.market_session,
            account_scope=body.account_scope,
            report_type=body.report_type,
            generator_version=body.generator_version,
            policy_version=body.policy_version,
            status=body.status,  # type: ignore[arg-type]
            created_by_profile=body.created_by_profile,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_composition_request", "validation": exc.errors()},
        ) from exc

    svc = HermesCompositionIngestService(db)
    try:
        report = await svc.ingest_composition(ingest_request)
    except HermesCompositionIngestError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "snapshot_bundle_not_found",
                "snapshot_bundle_uuid": str(body.composition.snapshot_bundle_uuid),
                "message": str(exc),
            },
        ) from exc
    await db.commit()
    return {
        "success": True,
        "report_uuid": str(report.report_uuid),
        "idempotency_key": report.idempotency_key,
        "snapshot_bundle_uuid": str(body.composition.snapshot_bundle_uuid),
        "status": report.status,
        "items_count": len(body.composition.items),
    }


# ---------------------------------------------------------------------------
# POST /symbol-reports  (ROB-301)
# ---------------------------------------------------------------------------


@router.post("/symbol-reports")
async def symbol_reports_ingest(
    body: HermesSymbolReportsIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Hermes push-only ingest of symbol-scoped intermediate reports (D9).

    Under the same ``/trading/api/investment-reports/hermes`` prefix as the
    other Hermes endpoints, so the AuthMiddleware token branch (403 if the
    ingest token is unset, 401 if wrong) and the enable gate both apply.
    """
    _require_enabled()

    svc = SymbolIntermediateReportIngestService(db)
    try:
        response = await svc.ingest_from_hermes(body)
    except SymbolReportIngestError as exc:
        await db.rollback()
        http_status = _INGEST_ERROR_HTTP_STATUS.get(
            exc.code, status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=http_status,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return {
        "success": True,
        "run_uuid": str(response.run.run_uuid),
        "run_status": response.run.status,
        "snapshot_bundle_uuid": str(response.run.snapshot_bundle_uuid),
        "symbol_reports": [
            {
                "symbol": r.symbol,
                "symbol_report_uuid": str(r.report.symbol_report_uuid),
                "decision_bucket": r.report.decision_bucket,
                "verdict": r.report.verdict,
                "artifact_version": r.report.artifact_version,
                "idempotent_existing": r.idempotent_existing,
            }
            for r in response.results
        ],
    }


@router.post("/dimension-reports")
async def dimension_reports_ingest(
    body: HermesDimensionReportsIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Hermes push-only ingest of per-dimension analyst reports (ROB-306).

    Same ``/trading/api/investment-reports/hermes`` prefix, so the
    AuthMiddleware token branch (403 unset / 401 wrong) + enable gate apply.
    """
    _require_enabled()

    svc = DimensionReportIngestService(db)
    try:
        response = await svc.ingest_from_hermes(body)
    except DimensionReportIngestError as exc:
        await db.rollback()
        http_status = _INGEST_ERROR_HTTP_STATUS.get(
            exc.code, status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=http_status,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return {
        "success": True,
        "run_uuid": str(response.run.run_uuid),
        "dimension_reports": [
            {
                "dimension": r.dimension,
                "dimension_report_uuid": str(r.report.dimension_report_uuid),
                "market": r.report.market,
                "symbol": r.report.symbol,
                "stance": r.report.stance,
                "confidence": r.report.confidence,
                "artifact_version": r.report.artifact_version,
                "idempotent_existing": r.idempotent_existing,
            }
            for r in response.results
        ],
    }


# Unused parameter — kept on the signature for symmetry with the MCP
# tool's ``snapshot_bundle_uuid: str`` argument shape. Suppresses the
# false-positive "imported but unused" warning that previously hit
# ``Path`` in the docs example.
_UNUSED_PATH_ANNOTATION = Path  # noqa: F841

"""ROB-265 — Investment-report read endpoints (Plan 3).

GET-only HTTP surface over the Plan 2 query service. Writes
(create / decide / activate) go through MCP in Plan 3; HTTP write
endpoints can be added later if needed.

Auth uses the same ``get_authenticated_user`` dependency as the legacy
``analysis_reports`` router. The route exists under both
``/trading/api/...`` and ``/invest/api/...`` so the existing frontend
proxy patterns keep working.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.investment_reports import (
    AccountScopeLiteral,
    InvestmentReportBundle,
    InvestmentReportItemDecisionResponse,
    InvestmentReportItemResponse,
    InvestmentReportListResponse,
    InvestmentReportNewsCitationResponse,
    InvestmentReportResponse,
    InvestmentWatchAlertResponse,
    InvestmentWatchEventResponse,
    MarketLiteral,
    MarketSessionLiteral,
    PreviousReportContextResponse,
    ReportSnapshotBundleResponse,
    ReportSnapshotDetailResponse,
    ReportStatusLiteral,
)
from app.services.investment_reports.action_packet import build_action_packet
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.review_sections import build_review_sections

router = APIRouter(tags=["investment-reports"])


def _build_query_service(
    db: AsyncSession = Depends(get_db),
) -> InvestmentReportQueryService:
    return InvestmentReportQueryService(db)


def _serialise_bundle(bundle: dict) -> InvestmentReportBundle:
    items = bundle["items"]
    # ROB-554 — attach reverse-looked-up linked orders (set post-validation;
    # the ORM item row has no such attribute). Missing key => legacy/no orders.
    linked_by_uuid = bundle.get("linked_orders_by_item_uuid", {})
    item_responses = []
    for it in items:
        resp = InvestmentReportItemResponse.model_validate(it)
        resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))
        item_responses.append(resp)
    decisions_by_item_uuid: dict[str, list[InvestmentReportItemDecisionResponse]] = {}
    for item in items:
        rows = bundle["decisions_by_item"].get(item.id, [])
        decisions_by_item_uuid[str(item.item_uuid)] = [
            InvestmentReportItemDecisionResponse.model_validate(d) for d in rows
        ]

    _HELD_BUCKETS = {"open_action", "risk_watch", "completed_or_existing"}
    item_groups: dict[str, list[InvestmentReportItemResponse]] = {}
    for it in item_responses:
        item_groups.setdefault(it.decision_bucket or "unclassified", []).append(it)
    rollup = {
        "new_candidate": [
            i for i in item_responses if i.decision_bucket == "new_buy_candidate"
        ],
        "held_action": [
            i for i in item_responses if i.decision_bucket in _HELD_BUCKETS
        ],
    }

    report_response = InvestmentReportResponse.model_validate(bundle["report"])
    # ROB-322 — additive five-section review projection (view-layer only).
    review_sections = build_review_sections(
        item_responses, report_response.snapshot_report_diagnostics
    )

    # ROB-335 — additive intraday ActionPacket projection (view-layer only).
    action_packet = build_action_packet(
        item_responses, report_response.snapshot_report_diagnostics
    )

    return InvestmentReportBundle(
        report=report_response,
        items=item_responses,
        decisions_by_item_uuid=decisions_by_item_uuid,
        alerts=[
            InvestmentWatchAlertResponse.model_validate(a) for a in bundle["alerts"]
        ],
        events=[
            InvestmentWatchEventResponse.model_validate(e) for e in bundle["events"]
        ],
        item_groups=item_groups,
        decision_rollup=rollup,
        review_sections=review_sections,
        action_packet=action_packet,
        news_citations=[
            InvestmentReportNewsCitationResponse.model_validate(c)
            for c in bundle["news_citations"]
        ],
    )


def _serialise_context(ctx: dict) -> PreviousReportContextResponse:
    return PreviousReportContextResponse(
        prior_reports=[
            InvestmentReportResponse.model_validate(r) for r in ctx["prior_reports"]
        ],
        unresolved_deferred_items=[
            InvestmentReportItemResponse.model_validate(it)
            for it in ctx["unresolved_deferred_items"]
        ],
        active_watches=[
            InvestmentWatchAlertResponse.model_validate(a)
            for a in ctx["active_watches"]
        ],
        triggered_events=[
            InvestmentWatchEventResponse.model_validate(e)
            for e in ctx["triggered_events"]
        ],
        recent_decisions=[
            InvestmentReportItemDecisionResponse.model_validate(d)
            for d in ctx["recent_decisions"]
        ],
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports",
    response_model=InvestmentReportListResponse,
    summary="List investment reports (ROB-265)",
)
@router.get(
    "/invest/api/investment-reports",
    response_model=InvestmentReportListResponse,
    summary="List investment reports for the /invest dashboard (ROB-265)",
)
async def list_investment_reports(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
    market: MarketLiteral | None = None,
    market_session: MarketSessionLiteral | None = None,
    account_scope: AccountScopeLiteral | None = None,
    status_filter: Annotated[ReportStatusLiteral | None, Query(alias="status")] = None,
    report_type: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> InvestmentReportListResponse:
    rows = await service.list_reports(
        market=market,
        market_session=market_session,
        account_scope=account_scope,
        status=status_filter,
        report_type=report_type,
        limit=limit,
    )
    return InvestmentReportListResponse(
        reports=[InvestmentReportResponse.model_validate(r) for r in rows]
    )


# ---------------------------------------------------------------------------
# Previous-report context (declared BEFORE /{report_uuid} so the literal
# path "context" doesn't get shadowed by the UUID-parameter route).
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports/context",
    response_model=PreviousReportContextResponse,
    summary="Previous-report context for the next-report generator (ROB-265)",
)
async def get_previous_report_context(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
    market: MarketLiteral = Query(..., description="kr | us | crypto"),
    market_session: MarketSessionLiteral | None = None,
    account_scope: AccountScopeLiteral | None = None,
    report_type: str | None = None,
    exclude_report_uuid: UUID | None = None,
    n_prior: int = Query(default=3, ge=1, le=10),
) -> PreviousReportContextResponse:
    ctx = await service.previous_report_context(
        market=market,
        market_session=market_session,
        account_scope=account_scope,
        report_type=report_type,
        exclude_report_uuid=exclude_report_uuid,
        n_prior=n_prior,
    )
    return _serialise_context(ctx)


# ---------------------------------------------------------------------------
# Get one bundle
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports/{report_uuid}",
    response_model=InvestmentReportBundle,
    summary="Get one investment report bundle (ROB-265)",
)
@router.get(
    "/invest/api/investment-reports/{report_uuid}",
    response_model=InvestmentReportBundle,
    summary="Get one investment report bundle for the /invest dashboard (ROB-265)",
)
async def get_investment_report(
    report_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
) -> InvestmentReportBundle:
    bundle = await service.get_bundle(report_uuid)
    if bundle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return _serialise_bundle(bundle)


# ---------------------------------------------------------------------------
# ROB-275 — Snapshot evidence viewer (read-only).
#
# These endpoints surface the bundle of snapshots actually linked to a
# report (membership via investment_snapshot_bundle_items). They are NOT
# gated by ``INVESTMENT_SNAPSHOTS_MCP_ENABLED``: gating is by data
# presence — the bundle endpoint returns a legacy/no_snapshot shape when
# report.snapshot_bundle_uuid is null, and the detail endpoint returns
# 404 on missing report / missing bundle / non-member snapshot.
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports/{report_uuid}/snapshot-bundle",
    response_model=ReportSnapshotBundleResponse,
    summary="Get snapshot bundle linked to an investment report (ROB-275)",
)
@router.get(
    "/invest/api/investment-reports/{report_uuid}/snapshot-bundle",
    response_model=ReportSnapshotBundleResponse,
    summary="Get snapshot bundle for /invest report viewer (ROB-275)",
)
async def get_investment_report_snapshot_bundle(
    report_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
) -> ReportSnapshotBundleResponse:
    result = await service.get_report_snapshot_bundle(report_uuid)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="report_not_found"
        )
    return result


@router.get(
    "/trading/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}",
    response_model=ReportSnapshotDetailResponse,
    summary="Get one snapshot's payload via its report (ROB-275)",
)
@router.get(
    "/invest/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}",
    response_model=ReportSnapshotDetailResponse,
    summary="Get one snapshot's payload for /invest report viewer (ROB-275)",
)
async def get_investment_report_snapshot_detail(
    report_uuid: UUID,
    snapshot_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[InvestmentReportQueryService, Depends(_build_query_service)],
) -> ReportSnapshotDetailResponse:
    detail = await service.get_report_snapshot_detail(report_uuid, snapshot_uuid)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="snapshot_not_found_or_not_in_report_bundle",
        )
    return detail


# ---------------------------------------------------------------------------
# Snapshot-backed advisory report generator (ROB-273)
#
# Opt-in entrypoint that automates the manual snapshot-bundle + report
# flow validated in production. The generator + collectors are read-only
# (no broker mutation, no watch activation, no scheduler registration).
# Default behaviour: 503 unless ``SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
# is set. Existing GET list/detail endpoints are unchanged.
# ---------------------------------------------------------------------------
from app.core.config import settings  # noqa: E402  — keep router-local
from app.services.action_report.snapshot_backed.generator import (  # noqa: E402
    PublishBlockedByStaleGateError,
    SnapshotBackedReportGenerator,
    SnapshotBackedReportGeneratorError,
)
from app.services.action_report.snapshot_backed.request import (  # noqa: E402
    ReportGenerationRequest,
    ReportGenerationResponse,
)


@router.post(
    "/trading/api/investment-reports/snapshot-backed",
    response_model=ReportGenerationResponse,
    summary="Generate a snapshot-backed advisory report (ROB-273, opt-in)",
)
async def generate_snapshot_backed_report(
    payload: ReportGenerationRequest,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportGenerationResponse:
    if not settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="snapshot_backed_report_generator_disabled",
        )
    generator = SnapshotBackedReportGenerator(db)
    try:
        response = await generator.generate(payload)
    except PublishBlockedByStaleGateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "publish_blocked_by_stale_gate",
                "reason": str(exc),
                "bundle_status": exc.bundle_status,
                "freshness_summary": exc.freshness_summary,
            },
        ) from exc
    except SnapshotBackedReportGeneratorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    await db.commit()
    return response

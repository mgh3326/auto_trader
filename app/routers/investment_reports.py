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
    InvestmentReportResponse,
    InvestmentWatchAlertResponse,
    InvestmentWatchEventResponse,
    MarketLiteral,
    MarketSessionLiteral,
    PreviousReportContextResponse,
    ReportStatusLiteral,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)

router = APIRouter(tags=["investment-reports"])


def _build_query_service(
    db: AsyncSession = Depends(get_db),
) -> InvestmentReportQueryService:
    return InvestmentReportQueryService(db)


def _serialise_bundle(bundle: dict) -> InvestmentReportBundle:
    items = bundle["items"]
    item_responses = [InvestmentReportItemResponse.model_validate(it) for it in items]
    decisions_by_item_uuid: dict[str, list[InvestmentReportItemDecisionResponse]] = {}
    for item in items:
        rows = bundle["decisions_by_item"].get(item.id, [])
        decisions_by_item_uuid[str(item.item_uuid)] = [
            InvestmentReportItemDecisionResponse.model_validate(d) for d in rows
        ]
    return InvestmentReportBundle(
        report=InvestmentReportResponse.model_validate(bundle["report"]),
        items=item_responses,
        decisions_by_item_uuid=decisions_by_item_uuid,
        alerts=[
            InvestmentWatchAlertResponse.model_validate(a) for a in bundle["alerts"]
        ],
        events=[
            InvestmentWatchEventResponse.model_validate(e) for e in bundle["events"]
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

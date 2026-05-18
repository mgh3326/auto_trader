"""MCP handlers for ROB-265 investment_reports.

Six tools mirror the Linear issue list. Each opens its own
``AsyncSessionLocal``, validates the incoming request via the Plan 2
Pydantic schema, calls the service, commits, and returns the serialised
response. No broker mutation, no scanner side effects — Plan 4 owns
that surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.core.db import AsyncSessionLocal
from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    InvestmentReportActivateWatchResponse,
    InvestmentReportBundle,
    InvestmentReportCreateResponse,
    InvestmentReportDecideItemResponse,
    InvestmentReportItemDecisionResponse,
    InvestmentReportItemResponse,
    InvestmentReportListResponse,
    InvestmentReportResponse,
    InvestmentWatchAlertResponse,
    InvestmentWatchEventResponse,
    PreviousReportContextResponse,
    RecordDecisionRequest,
)
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService

if TYPE_CHECKING:
    from fastmcp import FastMCP

INVESTMENT_REPORT_TOOL_NAMES: set[str] = {
    "investment_report_create",
    "investment_report_list",
    "investment_report_get",
    "investment_report_decide_item",
    "investment_report_activate_watch",
    "investment_report_context_get",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialise_bundle(bundle: dict) -> InvestmentReportBundle:
    items = bundle["items"]
    decisions_by_item_uuid: dict[str, list[InvestmentReportItemDecisionResponse]] = {}
    for item in items:
        rows = bundle["decisions_by_item"].get(item.id, [])
        decisions_by_item_uuid[str(item.item_uuid)] = [
            InvestmentReportItemDecisionResponse.model_validate(d) for d in rows
        ]
    return InvestmentReportBundle(
        report=InvestmentReportResponse.model_validate(bundle["report"]),
        items=[InvestmentReportItemResponse.model_validate(it) for it in items],
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
# investment_report_create
# ---------------------------------------------------------------------------
async def investment_report_create_impl(
    report_type: str,
    market: str,
    summary: str,
    created_by_profile: str,
    title: str,
    kst_date: str,
    items: list[dict[str, Any]] | None = None,
    market_session: str | None = None,
    account_scope: str | None = None,
    execution_mode: str = "advisory_only",
    risk_summary: str | None = None,
    thesis_text: str | None = None,
    no_action_note: str | None = None,
    market_snapshot: dict[str, Any] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    previous_report_uuid: str | None = None,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
    valid_until: str | None = None,
    published_at: str | None = None,
    generator_version: str = "v1",
) -> dict:
    payload: dict[str, Any] = {
        "report_type": report_type,
        "market": market,
        "market_session": market_session,
        "account_scope": account_scope,
        "execution_mode": execution_mode,
        "created_by_profile": created_by_profile,
        "title": title,
        "summary": summary,
        "risk_summary": risk_summary,
        "thesis_text": thesis_text,
        "no_action_note": no_action_note,
        "market_snapshot": market_snapshot or {},
        "portfolio_snapshot": portfolio_snapshot or {},
        "previous_report_uuid": previous_report_uuid,
        "status": status,
        "metadata": metadata or {},
        "valid_until": valid_until,
        "published_at": published_at,
        "items": [IngestReportItem.model_validate(it) for it in (items or [])],
        "generator_version": generator_version,
        "kst_date": kst_date,
    }
    request = IngestReportRequest.model_validate(payload)

    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        # Probe for idempotency BEFORE the service call so we can tell the
        # caller whether the row was newly created or returned as-is.
        from app.services.investment_reports.idempotency import report_key

        composed_key = report_key(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            kst_date=request.kst_date,
            generator_version=request.generator_version,
        )
        pre_existing = await repo.get_report_by_idempotency_key(composed_key)
        is_new = pre_existing is None

        service = InvestmentReportIngestionService(db, repository=repo)
        report = await service.ingest(request)
        await db.commit()

        response = InvestmentReportCreateResponse(
            idempotent=not is_new,
            report=InvestmentReportResponse.model_validate(report),
        )
    return response.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# investment_report_list
# ---------------------------------------------------------------------------
async def investment_report_list_impl(
    market: str | None = None,
    market_session: str | None = None,
    account_scope: str | None = None,
    status: str | None = None,
    report_type: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(int(limit), 100))
    async with AsyncSessionLocal() as db:
        service = InvestmentReportQueryService(db)
        rows = await service.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            limit=capped,
        )
        response = InvestmentReportListResponse(
            reports=[InvestmentReportResponse.model_validate(r) for r in rows]
        )
    return {"success": True, **response.model_dump(mode="json", by_alias=True)}


# ---------------------------------------------------------------------------
# investment_report_get
# ---------------------------------------------------------------------------
async def investment_report_get_impl(report_uuid: str) -> dict:
    parsed = UUID(report_uuid)
    async with AsyncSessionLocal() as db:
        service = InvestmentReportQueryService(db)
        bundle = await service.get_bundle(parsed)
        if bundle is None:
            return {"success": False, "error": "not_found"}
        serialised = _serialise_bundle(bundle)
    return {"success": True, **serialised.model_dump(mode="json", by_alias=True)}


# ---------------------------------------------------------------------------
# investment_report_decide_item
# ---------------------------------------------------------------------------
async def investment_report_decide_item_impl(
    item_uuid: str,
    decision: str,
    actor: str,
    decision_note: str | None = None,
    approved_payload_snapshot: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    request = RecordDecisionRequest.model_validate(
        {
            "item_uuid": item_uuid,
            "decision": decision,
            "actor": actor,
            "decision_note": decision_note,
            "approved_payload_snapshot": approved_payload_snapshot,
            "idempotency_key": idempotency_key,
        }
    )
    async with AsyncSessionLocal() as db:
        decisions_svc = InvestmentReportDecisionService(db)
        repo = InvestmentReportsRepository(db)
        decision_row = await decisions_svc.record(request)
        item_row = await repo.get_item_by_uuid(request.item_uuid)
        await db.commit()

        response = InvestmentReportDecideItemResponse(
            decision=InvestmentReportItemDecisionResponse.model_validate(decision_row),
            item=InvestmentReportItemResponse.model_validate(item_row),
        )
    return response.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# investment_report_activate_watch
# ---------------------------------------------------------------------------
async def investment_report_activate_watch_impl(
    item_uuid: str,
    actor: str,
    idempotency_key: str | None = None,
) -> dict:
    request = ActivateWatchRequest.model_validate(
        {
            "item_uuid": item_uuid,
            "actor": actor,
            "idempotency_key": idempotency_key,
        }
    )
    async with AsyncSessionLocal() as db:
        activation_svc = WatchActivationService(db)
        repo = InvestmentReportsRepository(db)
        alert_row = await activation_svc.activate(request)
        item_row = await repo.get_item_by_uuid(request.item_uuid)
        await db.commit()

        response = InvestmentReportActivateWatchResponse(
            alert=InvestmentWatchAlertResponse.model_validate(alert_row),
            item=InvestmentReportItemResponse.model_validate(item_row),
        )
    return response.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# investment_report_context_get
# ---------------------------------------------------------------------------
async def investment_report_context_get_impl(
    market: str,
    market_session: str | None = None,
    account_scope: str | None = None,
    report_type: str | None = None,
    exclude_report_uuid: str | None = None,
    n_prior: int = 3,
) -> dict:
    capped = max(1, min(int(n_prior), 10))
    exclude_uuid = UUID(exclude_report_uuid) if exclude_report_uuid else None
    async with AsyncSessionLocal() as db:
        service = InvestmentReportQueryService(db)
        ctx = await service.previous_report_context(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            report_type=report_type,
            exclude_report_uuid=exclude_uuid,
            n_prior=capped,
        )
        serialised = _serialise_context(ctx)
    return {"success": True, **serialised.model_dump(mode="json", by_alias=True)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_investment_report_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="investment_report_create",
        description=(
            "Persist one ROB-265 investment_report bundle (report + items). "
            "Idempotent on (report_type, market, market_session, account_scope, "
            "execution_mode, kst_date, generator_version). "
            "No broker / order submission is performed."
        ),
    )(investment_report_create_impl)
    mcp.tool(
        name="investment_report_list",
        description=(
            "List investment_reports filtered by market / market_session / "
            "account_scope / status / report_type. limit clamped to 1..100."
        ),
    )(investment_report_list_impl)
    mcp.tool(
        name="investment_report_get",
        description=(
            "Return one investment_report bundle by report_uuid — report + "
            "items + decisions_by_item_uuid + alerts + recent events."
        ),
    )(investment_report_get_impl)
    mcp.tool(
        name="investment_report_decide_item",
        description=(
            "Record an operator decision on one investment_report_item. "
            "Idempotent per (item_uuid, verb, actor) by default; pass "
            "idempotency_key to override. partial_approve requires a "
            "non-empty approved_payload_snapshot."
        ),
    )(investment_report_decide_item_impl)
    mcp.tool(
        name="investment_report_activate_watch",
        description=(
            "Activate an approved watch item into investment_watch_alerts "
            "as an immutable activation snapshot. Idempotent per source item."
        ),
    )(investment_report_activate_watch_impl)
    mcp.tool(
        name="investment_report_context_get",
        description=(
            "Return previous-report context for the next-report generator: "
            "prior_reports, unresolved_deferred_items, active_watches, "
            "triggered_events, recent_decisions. n_prior clamped to 1..10."
        ),
    )(investment_report_context_get_impl)


__all__ = [
    "INVESTMENT_REPORT_TOOL_NAMES",
    "investment_report_activate_watch_impl",
    "investment_report_context_get_impl",
    "investment_report_create_impl",
    "investment_report_decide_item_impl",
    "investment_report_get_impl",
    "investment_report_list_impl",
    "register_investment_report_tools",
]

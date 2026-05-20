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
    "investment_report_generate_from_bundle",
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


def _serialise_context(
    ctx: dict,
    *,
    pending_orders: list[dict[str, Any]] | None = None,
) -> PreviousReportContextResponse:
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
        pending_orders=pending_orders,
    )


# ROB-274 — default account_scope per market when the caller didn't supply
# one. The pending_orders collector requires a concrete scope (kis_live /
# upbit_live) to know which broker to query. Keep this mirroring the
# collector's own supported pairs in pending_orders.py.
_DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",
    "crypto": "upbit_live",
}


async def _collect_pending_orders_snapshot(
    db: Any,
    *,
    market: str,
    account_scope: str | None,
) -> list[dict[str, Any]] | None:
    """ROB-274 — fetch the pending_orders snapshot for the context response.

    Returns ``None`` when the collector is missing, reports unavailable/
    stale, or the (market, account_scope) pair isn't supported. Returns
    a (possibly empty) list when the broker reported successfully.
    """
    from app.services.action_report.snapshot_backed.collectors.registry import (
        production_collector_registry,
    )
    from app.services.investment_snapshots.collectors import CollectorRequest

    effective_scope = account_scope or _DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if effective_scope is None:
        return None

    try:
        registry = production_collector_registry(db)
    except Exception:  # noqa: BLE001 — registry must never raise to caller
        return None
    collector = registry.get("pending_orders")
    if collector is None:
        return None

    try:
        results = await collector.collect(
            CollectorRequest(
                market=market,  # type: ignore[arg-type]
                account_scope=effective_scope,  # type: ignore[arg-type]
                policy_snapshot={},
            )
        )
    except Exception:  # noqa: BLE001 — collector contract is fail-open
        return None
    if not results:
        return None
    result = results[0]
    if result.freshness_status in ("unavailable", "hard_stale"):
        return None
    payload = result.payload_json or {}
    orders = payload.get("pending_orders")
    if orders is None:
        return []
    return list(orders)


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
        # ROB-274 — enrich the response with the pending_orders snapshot so
        # next-report drafters see what's already open at the broker. The
        # helper fails open: any collector trouble surfaces as ``None``.
        pending_orders = await _collect_pending_orders_snapshot(
            db, market=market, account_scope=account_scope
        )
        serialised = _serialise_context(ctx, pending_orders=pending_orders)
    return {"success": True, **serialised.model_dump(mode="json", by_alias=True)}


# ---------------------------------------------------------------------------
# investment_report_generate_from_bundle (ROB-273)
# ---------------------------------------------------------------------------
async def investment_report_generate_from_bundle_impl(
    market: str,
    account_scope: str,
    title: str,
    summary: str,
    kst_date: str,
    created_by_profile: str,
    items: list[dict[str, Any]] | None = None,
    risk_summary: str | None = None,
    thesis_text: str | None = None,
    no_action_note: str | None = None,
    status: str = "published",
    metadata: dict[str, Any] | None = None,
    valid_until: str | None = None,
    published_at: str | None = None,
    previous_report_uuid: str | None = None,
    policy_version: str = "intraday_action_report_v1",
    generator_version: str = "v2-snapshot-backed",
    report_type: str = "snapshot_backed_advisory_v1",
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "claude_code",
) -> dict:
    """Generate a snapshot-backed advisory report.

    Opt-in entrypoint for ROB-273. Default-off: the harness returns
    ``success=False`` unless ``SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
    is set on the deployment. The generator never mutates broker /
    order / watch state — see docs for the read-only guarantees.
    """
    from app.core.config import settings
    from app.services.action_report.snapshot_backed.generator import (
        PublishBlockedByStaleGateError,
        SnapshotBackedReportGenerator,
        SnapshotBackedReportGeneratorError,
    )
    from app.services.action_report.snapshot_backed.request import (
        ReportGenerationRequest,
    )

    if not settings.SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED:
        return {
            "success": False,
            "error": "snapshot_backed_report_generator_disabled",
            "hint": (
                "Set SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true on the "
                "MCP host to enable this tool."
            ),
        }

    payload: dict[str, Any] = {
        "market": market,
        "account_scope": account_scope,
        "policy_version": policy_version,
        "status": status,
        "requested_by": requested_by,
        "report_type": report_type,
        "generator_version": generator_version,
        "created_by_profile": created_by_profile,
        "title": title,
        "summary": summary,
        "kst_date": kst_date,
        "risk_summary": risk_summary,
        "thesis_text": thesis_text,
        "no_action_note": no_action_note,
        "items": [IngestReportItem.model_validate(it) for it in (items or [])],
        "previous_report_uuid": previous_report_uuid,
        "valid_until": valid_until,
        "published_at": published_at,
        "metadata": metadata or {},
        "symbols": symbols,
        "candidate_limit": candidate_limit,
    }
    request = ReportGenerationRequest.model_validate(payload)

    async with AsyncSessionLocal() as db:
        generator = SnapshotBackedReportGenerator(db)
        try:
            response = await generator.generate(request)
        except PublishBlockedByStaleGateError as exc:
            return {
                "success": False,
                "error": "publish_blocked_by_stale_gate",
                "reason": str(exc),
                "bundle_status": exc.bundle_status,
                "freshness_summary": exc.freshness_summary,
            }
        except SnapshotBackedReportGeneratorError as exc:
            return {"success": False, "error": str(exc)}
        await db.commit()

    return {"success": True, **response.model_dump(mode="json")}


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
    mcp.tool(
        name="investment_report_generate_from_bundle",
        description=(
            "ROB-273 — generate a snapshot-backed advisory investment_report "
            "end-to-end. Ensures (or reuses) a snapshot bundle, runs the "
            "read-only collector registry, normalises payloads, and persists "
            "the report with snapshot metadata. Opt-in: returns "
            "{success:false, error:'snapshot_backed_report_generator_disabled'} "
            "unless SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED is true. "
            "No broker / order / watch mutation."
        ),
    )(investment_report_generate_from_bundle_impl)


__all__ = [
    "INVESTMENT_REPORT_TOOL_NAMES",
    "investment_report_activate_watch_impl",
    "investment_report_context_get_impl",
    "investment_report_create_impl",
    "investment_report_decide_item_impl",
    "investment_report_generate_from_bundle_impl",
    "investment_report_get_impl",
    "investment_report_list_impl",
    "register_investment_report_tools",
]

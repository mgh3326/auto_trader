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

from pydantic import ValidationError

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

# ROB-352 — mirror of the generator's canonical market/account_scope pairs.
# A drift-guard test asserts this equals ``generator._MARKET_ACCOUNT_PAIRS``.
# Kept here as a literal so the handler can fail closed BEFORE importing or
# constructing the generator.
_SUPPORTED_MARKET_ACCOUNT_PAIRS: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",
    "crypto": "upbit_live",
}

# ROB-352 — persisted-layer market_session vocabulary (mirrors
# ``MarketSessionLiteral`` in app/schemas/investment_reports.py). Validated in
# the handler so an invalid session returns a structured error instead of an
# uncaught ValidationError from ReportGenerationRequest.
_ALLOWED_MARKET_SESSIONS: tuple[str, ...] = ("regular", "nxt", "pre", "post", "24x7")

GENERATE_FROM_BUNDLE_DESCRIPTION = (
    "ROB-273/ROB-352 — generate a snapshot-backed advisory investment_report "
    "end-to-end. Ensures (or reuses) a snapshot bundle, runs the read-only "
    "collector registry, normalises payloads, and persists the report with "
    "snapshot metadata. Opt-in: returns {success:false, "
    "error:'snapshot_backed_report_generator_disabled'} unless "
    "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED is true. "
    "Supported market/account_scope pairs ONLY: kr/kis_live, us/kis_live, "
    "crypto/upbit_live — any other pair fails closed with "
    "error:'unsupported_account_scope' (use the Hermes composition path for "
    "alpaca_paper). user_id is auto-resolved from MCP_USER_ID when omitted so "
    "kis_live/upbit_live portfolios are readable; pass user_id to override. "
    "Optional market_session (regular|nxt|pre|post|24x7) refines US/KR session "
    "reporting and is part of the idempotency key. items[] each require: "
    "client_item_key, item_kind (action|watch|risk), intent (buy_review|"
    "sell_review|risk_review|trend_recovery_review|rebalance_review), rationale; "
    "watch items also need watch_condition+valid_until unless operation='review'. "
    "Invalid items return error:'invalid_items' naming the offending index/field. "
    "Deterministic regeneration: by default an existing report for the same key "
    "is RETURNED FROM THE STORED ROW (reused_existing=true); pass "
    "overwrite_existing=true with overwrite_reason to transactionally replace it. "
    "No broker / order / watch mutation."
)


def _default_generator_user_id() -> int:
    """ROB-352 — resolve the default operator user_id the same way the
    portfolio/holdings tools do (``MCP_USER_ID`` env, default 1), so a
    kis_live/upbit_live report no longer silently degrades to
    portfolio=unavailable when the caller omits user_id.
    """
    from app.mcp_server.tooling.shared import MCP_USER_ID

    return MCP_USER_ID


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
    draft_policy: str = "exclude",
) -> dict:
    capped = max(1, min(int(n_prior), 10))
    # Fail closed: an unknown policy (incl. a hallucinated "all") falls back to
    # "exclude" so the tool never over-includes smoke drafts.
    policy = draft_policy if draft_policy in {"exclude", "advisory_only"} else "exclude"
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
            draft_policy=policy,
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
    market_session: str | None = None,
    symbols: list[str] | None = None,
    candidate_limit: int | None = None,
    requested_by: str = "claude_code",
    user_id: int | None = None,
    overwrite_existing: bool = False,
    overwrite_reason: str | None = None,
) -> dict:
    """Generate a snapshot-backed advisory report.

    Opt-in entrypoint for ROB-273. Default-off: the harness returns
    ``success=False`` unless ``SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED``
    is set on the deployment. The generator never mutates broker /
    order / watch state — see docs for the read-only guarantees.

    ROB-318/ROB-352 — ``user_id`` is forwarded to ``ReportGenerationRequest``
    so the ``kis_live`` portfolio collector can read live KIS holdings/cash.
    When omitted it is now resolved to the MCP default (``MCP_USER_ID``, like
    ``get_holdings``) for the supported live scopes, and the resolved id is
    returned as ``resolved_user_id`` — pass an explicit ``user_id`` to override.
    (Previously, omitting it stayed ``None`` and fail-closed the portfolio to
    ``unavailable``, forcing a misleading no_action.)
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
    from app.services.investment_reports.ingestion import (
        ReportOverwriteBlockedError,
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

    # ROB-352 — fail closed on unsupported account scopes BEFORE building the
    # request, with an actionable error that names the supported pairs and
    # routes paper accounts to the Hermes composition path.
    expected_scope = _SUPPORTED_MARKET_ACCOUNT_PAIRS.get(market)
    if expected_scope is None or account_scope != expected_scope:
        return {
            "success": False,
            "error": "unsupported_account_scope",
            "market": market,
            "account_scope": account_scope,
            "supported_pairs": _SUPPORTED_MARKET_ACCOUNT_PAIRS,
            "hint": (
                "This snapshot-backed generator only collects live KIS/Upbit "
                "data. For alpaca_paper / paper:<name> reports use the Hermes "
                "composition path (investment_report_create_from_hermes_"
                "composition)."
            ),
        }

    # ROB-352 — validate market_session against the persisted vocabulary so an
    # invalid session returns a structured error instead of an uncaught
    # ValidationError deep in request construction.
    if market_session is not None and market_session not in _ALLOWED_MARKET_SESSIONS:
        return {
            "success": False,
            "error": "invalid_market_session",
            "market_session": market_session,
            "allowed": list(_ALLOWED_MARKET_SESSIONS),
        }

    # ROB-352 — validate items with per-item, per-field errors instead of a raw
    # ValidationError. Names the offending item index/client_item_key and the
    # failing field so callers fix it without reading backend code.
    validated_items: list[IngestReportItem] = []
    item_errors: list[dict[str, Any]] = []
    for index, raw in enumerate(items or []):
        # Guard non-dict entries (e.g. a bare string/list) so building the
        # error report itself never crashes on ``.get``.
        if not isinstance(raw, dict):
            item_errors.append(
                {
                    "index": index,
                    "client_item_key": None,
                    "errors": [
                        {
                            "field": "",
                            "message": (
                                f"item must be an object, got {type(raw).__name__}"
                            ),
                        }
                    ],
                }
            )
            continue
        try:
            validated_items.append(IngestReportItem.model_validate(raw))
        except ValidationError as exc:
            item_errors.append(
                {
                    "index": index,
                    "client_item_key": raw.get("client_item_key"),
                    "errors": [
                        {
                            "field": ".".join(str(p) for p in err["loc"]),
                            "message": err["msg"],
                        }
                        for err in exc.errors()
                    ],
                }
            )
    if item_errors:
        return {
            "success": False,
            "error": "invalid_items",
            "item_errors": item_errors,
            "required_fields": [
                "client_item_key",
                "item_kind",
                "intent",
                "rationale",
            ],
            "enums": {
                "item_kind": ["action", "watch", "risk"],
                "intent": [
                    "buy_review",
                    "sell_review",
                    "risk_review",
                    "trend_recovery_review",
                    "rebalance_review",
                ],
                "target_kind": ["asset", "index", "fx"],
                "side": ["buy", "sell"],
            },
            "notes": (
                "watch items require watch_condition + valid_until unless "
                "operation is 'review'; decision_bucket must be one of the "
                "DECISION_BUCKETS vocabulary."
            ),
        }

    # ROB-352 — a destructive overwrite must carry a non-empty reason (audit).
    # Pre-validate here so the caller gets a structured error rather than an
    # uncaught ValidationError from ReportGenerationRequest.
    if overwrite_existing and not (overwrite_reason and overwrite_reason.strip()):
        return {
            "success": False,
            "error": "overwrite_reason_required",
            "hint": (
                "Pass a non-empty overwrite_reason when overwrite_existing=true "
                "so the in-place regeneration is auditable."
            ),
        }

    # ROB-352 — resolve a default user_id for live account scopes so the
    # portfolio collector can read live holdings/cash (was a hidden required
    # dependency that degraded the bundle to failed → forced no_action).
    resolved_user_id = user_id if user_id is not None else _default_generator_user_id()

    payload: dict[str, Any] = {
        "market": market,
        "account_scope": account_scope,
        "market_session": market_session,
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
        "items": validated_items,
        "previous_report_uuid": previous_report_uuid,
        "valid_until": valid_until,
        "published_at": published_at,
        "metadata": metadata or {},
        "symbols": symbols,
        "candidate_limit": candidate_limit,
        "user_id": resolved_user_id,
        "overwrite_existing": overwrite_existing,
        "overwrite_reason": overwrite_reason,
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
        except ReportOverwriteBlockedError as exc:
            return {
                "success": False,
                "error": "overwrite_blocked_has_audit",
                "reason": str(exc),
                "report_uuid": str(exc.report_uuid),
                "decision_count": exc.decision_count,
                "active_alert_count": exc.active_alert_count,
                "hint": (
                    "This report has operator decisions or active watch alerts; "
                    "overwriting would destroy that audit. Supersede/revise via a "
                    "new report instead of regenerating in place."
                ),
            }
        except SnapshotBackedReportGeneratorError as exc:
            return {"success": False, "error": str(exc)}
        await db.commit()

    return {
        "success": True,
        "resolved_user_id": resolved_user_id,
        **response.model_dump(mode="json"),
    }


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
            "triggered_events, recent_decisions. n_prior clamped to 1..10. "
            "draft_policy (optional, default 'exclude'): 'exclude' drops all draft "
            "reports; 'advisory_only' admits genuine advisory drafts "
            "(created_by_profile=HERMES_ADVISOR) as prior context while still "
            "excluding smoke/test drafts. advisory reports persist as draft, so use "
            "'advisory_only' to chain the next delta report off the latest advisory "
            "baseline. (Unknown values fall back to 'exclude'.)"
        ),
    )(investment_report_context_get_impl)
    mcp.tool(
        name="investment_report_generate_from_bundle",
        description=GENERATE_FROM_BUNDLE_DESCRIPTION,
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

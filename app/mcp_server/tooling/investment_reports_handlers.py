"""MCP handlers for ROB-265 investment_reports.

Six tools mirror the Linear issue list. Each opens its own
``AsyncSessionLocal``, validates the incoming request via the Plan 2
Pydantic schema, calls the service, commits, and returns the serialised
response. No broker mutation, no scanner side effects — Plan 4 owns
that surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError

from app.core.db import AsyncSessionLocal
from app.schemas.investment_reports import (
    ActivateWatchRequest,
    AddReportItemsRequest,
    IngestReportItem,
    IngestReportRequest,
    InvestmentReportActivateWatchResponse,
    InvestmentReportBundle,
    InvestmentReportCreateResponse,
    InvestmentReportDecideItemResponse,
    InvestmentReportItemDecisionResponse,
    InvestmentReportItemResponse,
    InvestmentReportResponse,
    InvestmentWatchAlertResponse,
    InvestmentWatchEventResponse,
    PreviousReportContextResponse,
    RecordDecisionRequest,
    SetReportStatusRequest,
    UpdateDraftReportRequest,
)
from app.services import market_data as market_data_service
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.idempotency import kst_date_from_report_key
from app.services.investment_reports.ingestion import (
    DraftReportMutationBlockedError,
    InvestmentReportIngestionService,
)
from app.services.investment_reports.lite_grade import (
    build_lite_report_quality_summary,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
    _advisory_draft_profiles,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService
from app.services.investment_reports.watch_recommendation_policy import (
    ATR_PERIOD,
    LOOKBACK_DAYS,
    WatchPolicyInput,
    compute_watch_recommendation,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

INVESTMENT_REPORT_TOOL_NAMES: set[str] = {
    "investment_report_create",
    "investment_report_list",
    "investment_report_get",
    "investment_report_decide_item",
    "investment_report_activate_watch",
    "investment_report_context_get",
    "investment_report_delta_get",
    "investment_report_generate_from_bundle",
    "investment_watch_recommend",
    "investment_report_set_status",
    "investment_report_add_items",
    "investment_report_update",
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
    "ROB-347 budget controls: budget_basis defaults to 'available_usd'; "
    "'krw_orderable_reference' keeps KRW as reference-only and marks fx_required; "
    "'operator_budget_override' uses operator_budget_override_usd when present "
    "so USD=0 candidates remain visible without fabricating KRW→USD. "
    "Invalid items return error:'invalid_items' naming the offending index/field. "
    "Deterministic regeneration: by default an existing report for the same key "
    "is RETURNED FROM THE STORED ROW (reused_existing=true); pass "
    "overwrite_existing=true with overwrite_reason to transactionally replace it. "
    "No broker / order / watch mutation."
)


CREATE_DESCRIPTION = (
    "Persist one ROB-265 investment_report bundle (report + items). "
    "Idempotent on the 7-tuple (report_type, market, market_session, "
    "account_scope, execution_mode, kst_date, generator_version) — "
    "created_by_profile is NOT part of the key, so to mint a new row bump "
    "generator_version (recommended) or another keyed field, not "
    "created_by_profile. "
    "account_scope accepts kis_live | kis_mock | alpaca_paper | upbit_live "
    "(alpaca_paper IS accepted here; only "
    "investment_report_generate_from_bundle restricts to the live "
    "KIS/Upbit pairs and steers paper to the Hermes composition path). "
    "No broker / order submission is performed. "
    "items[] each require: client_item_key, item_kind (action|watch|risk), "
    "intent (buy_review|sell_review|risk_review|trend_recovery_review|"
    "rebalance_review), rationale. "
    "watch items also require watch_condition + valid_until unless "
    "operation='review'. "
    "Watch execution context: trigger_checklist is string[] and is copied into "
    "watch alert notifications. max_action is the structured execution-plan JSON; "
    "account_mode is required when max_action is present; required keys are side "
    "and exactly one of quantity or notional. "
    "Optional keys include amount_krw, limit_price, limit_price_hint, and "
    "ladder_level. planned_action in Hermes payloads is derived from max_action; "
    "do not send planned_action as an item key. "
    "target_kind (asset|index|fx, default 'asset') is a SEPARATE optional field "
    "— it is NOT item_kind. "
    "decision_bucket (optional) must be one of: new_buy_candidate, open_action, "
    "completed_or_existing, deferred_no_action, risk_watch. "
    "Optional structured evidence per item: evidence=[{source, metric, value, "
    "as_of, freshness}] (source required) plus item-level freshness "
    "(fresh|soft_stale|stale|unknown). item_evidence_lite quality grading reads "
    "these typed evidence[]/freshness fields, not arbitrary evidence_snapshot "
    "keys. evidence_snapshot remains an advanced raw JSON object for reserved "
    "read-side hints such as action_verdict/candidate_rank; typed fields are "
    "preferred for new inputs. "
    "Optional trade plan fields per item: entry_plan=[{label, price, quantity, "
    "notional, currency, condition, rationale}], stop_loss={price,...}, "
    "target_price={price,...}, linked_order_ids=[{broker, account_scope, "
    "order_no, odno, ledger_id, report_item_uuid, raw}]. These are advisory "
    "report fields only; they do not submit broker orders. Live audit linkage "
    "for new orders should still pass report_item_uuid to the order tool "
    "(ROB-473). Unknown item keys are rejected; put extension data under "
    "metadata or evidence_snapshot explicitly. "
    "For prior-report chaining set created_by_profile='CLAUDE_ADVISOR' so the "
    "draft is admitted by investment_report_context_get(draft_policy="
    "'advisory_only')."
)


ADD_ITEMS_DESCRIPTION = (
    "ROB-499 - append items to an existing draft investment_report without "
    "recreating the report. Draft-only: non-draft reports return "
    "error:'not_draft'. items[] use the same contract as investment_report_create; "
    "duplicate client_item_key rows are returned as existing items and are not "
    "rewritten. No broker / order / watch mutation. For watch items, trigger_checklist "
    "string[] and max_action execution-plan keys follow the same contract as "
    "investment_report_create: account_mode is required when max_action is present; "
    "max_action also requires side and exactly one of quantity or notional."
)

UPDATE_DESCRIPTION = (
    "ROB-499 - update draft report header fields (title, summary, risk_summary, "
    "thesis_text, no_action_note, market_snapshot, portfolio_snapshot, metadata, "
    "valid_until). Draft-only: non-draft reports return error:'not_draft'. "
    "Does not change report identity, status, previous_report_uuid, account scope, "
    "generator_version, or items. No broker / order / watch mutation."
)


CONTEXT_GET_DESCRIPTION = (
    "Return previous-report context for the next-report generator: "
    "prior_reports, unresolved_deferred_items, active_watches, "
    "triggered_events, recent_decisions. n_prior clamped to 1..10. "
    "draft_policy (optional, default 'exclude'): 'exclude' drops all draft "
    "reports; 'advisory_only' admits genuine advisory drafts "
    "(created_by_profile in HERMES_ADVISOR / CLAUDE_ADVISOR, plus any profiles "
    "configured via INVESTMENT_ADVISORY_DRAFT_PROFILES) as prior context while "
    "still excluding smoke/test drafts. advisory reports persist as draft, so "
    "use 'advisory_only' to chain the next delta report off the latest advisory "
    "baseline. (Unknown values fall back to 'exclude'.)"
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
    # ROB-554 — attach reverse-looked-up linked orders (parity with web serializer).
    linked_by_uuid = bundle.get("linked_orders_by_item_uuid", {})
    item_responses = []
    for it in items:
        resp = InvestmentReportItemResponse.model_validate(it)
        resp.linked_orders = linked_by_uuid.get(str(it.item_uuid))
        item_responses.append(resp)
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
async def _collect_pending_orders_snapshot(
    db: Any,
    *,
    market: str,
    account_scope: str | None,
) -> list[dict[str, Any]] | None:
    from app.mcp_server.tooling.pending_orders_snapshot import (
        collect_pending_orders_snapshot,
    )

    snapshot = await collect_pending_orders_snapshot(
        db,
        market=market,
        account_scope=account_scope,
    )
    return snapshot.orders


def _validate_report_items(
    raw_items: list[dict[str, Any]] | None,
) -> tuple[list[IngestReportItem], dict[str, Any] | None]:
    """Validate report items with per-item, per-field errors (ROB-458).

    Returns ``(validated_items, error_payload)``. ``error_payload`` is None on
    success; otherwise a structured MCP error dict naming EVERY offending item
    index / client_item_key / field so the caller fixes all violations in one
    round-trip. Shared by investment_report_create and
    investment_report_generate_from_bundle so the two cannot drift.
    """
    validated_items: list[IngestReportItem] = []
    item_errors: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items or []):
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
        return [], {
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
                "DECISION_BUCKETS vocabulary. target_kind is a SEPARATE optional "
                "field (asset|index|fx, default 'asset') — it is NOT item_kind."
                " trigger_checklist must be string[]; watch execution plans belong in "
                "max_action (required: side, account_mode, exactly one of "
                "quantity/notional; optional: amount_krw, limit_price, "
                "limit_price_hint, ladder_level), not in planned_action."
            ),
        }
    return validated_items, None


def _maybe_attach_lite_quality(
    request: IngestReportRequest,
) -> IngestReportRequest:
    """ROB-472 — attach a deterministic lite quality grade to advisory reports.

    Pure metadata (grade gates nothing). Only for advisory profiles; never
    clobbers caller-supplied diagnostics; fail-open (a helper error never blocks
    report creation). snapshot_freshness_summary/coverage_summary stay None so
    the published-report DB CHECK is never triggered.
    """
    if request.snapshot_report_diagnostics is not None:
        return request
    if request.created_by_profile not in _advisory_draft_profiles():
        return request
    try:
        summary = build_lite_report_quality_summary(request.items)
    except Exception:
        return request
    return request.model_copy(
        update={"snapshot_report_diagnostics": {"report_quality_summary": summary}}
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
    # ROB-458 — validate items with per-item, all-at-once errors BEFORE opening
    # a DB session, so a malformed call never gets a partial write or a raw
    # ValidationError. Mirrors investment_report_generate_from_bundle.
    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error

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
        "items": validated_items,
        "generator_version": generator_version,
        "kst_date": kst_date,
    }
    request = IngestReportRequest.model_validate(payload)
    # ROB-472 — advisory lite reports get a deterministic, evidence-derived
    # quality grade (display/audit metadata only). No-op for non-advisory
    # profiles and when the caller already supplied diagnostics.
    request = _maybe_attach_lite_quality(request)

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


async def investment_report_add_items_impl(
    report_uuid: str,
    items: list[dict[str, Any]] | None = None,
    actor: str | None = None,
) -> dict:
    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error
    try:
        request = AddReportItemsRequest.model_validate(
            {
                "report_uuid": report_uuid,
                "items": validated_items,
                "actor": actor,
            }
        )
    except ValidationError as exc:
        return {"success": False, "error": "invalid_request", "detail": str(exc)}

    async with AsyncSessionLocal() as db:
        service = InvestmentReportIngestionService(db)
        try:
            report, inserted, existing = await service.add_items_to_draft(
                report_uuid=request.report_uuid,
                items=request.items,
            )
        except DraftReportMutationBlockedError as exc:
            return {
                "success": False,
                "error": "not_draft",
                "report_uuid": str(exc.report_uuid),
                "status": exc.status,
            }
        if report is None:
            return {
                "success": False,
                "error": "not_found",
                "report_uuid": str(request.report_uuid),
            }
        await db.commit()

        return {
            "success": True,
            "report_uuid": str(report.report_uuid),
            "inserted_count": len(inserted),
            "existing_count": len(existing),
            "inserted_items": [
                InvestmentReportItemResponse.model_validate(it).model_dump(
                    mode="json", by_alias=True
                )
                for it in inserted
            ],
            "existing_items": [
                InvestmentReportItemResponse.model_validate(it).model_dump(
                    mode="json", by_alias=True
                )
                for it in existing
            ],
        }


async def investment_report_update_impl(
    report_uuid: str,
    title: str | None = None,
    summary: str | None = None,
    risk_summary: str | None = None,
    thesis_text: str | None = None,
    no_action_note: str | None = None,
    market_snapshot: dict[str, Any] | None = None,
    portfolio_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    valid_until: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> dict:
    try:
        request = UpdateDraftReportRequest.model_validate(
            {
                "report_uuid": report_uuid,
                "title": title,
                "summary": summary,
                "risk_summary": risk_summary,
                "thesis_text": thesis_text,
                "no_action_note": no_action_note,
                "market_snapshot": market_snapshot,
                "portfolio_snapshot": portfolio_snapshot,
                "metadata": metadata,
                "valid_until": valid_until,
                "actor": actor,
                "reason": reason,
            }
        )
    except ValidationError as exc:
        return {"success": False, "error": "invalid_request", "detail": str(exc)}

    updates = request.model_dump(
        exclude={"report_uuid", "actor", "reason"},
        exclude_none=True,
    )
    async with AsyncSessionLocal() as db:
        service = InvestmentReportIngestionService(db)
        try:
            report = await service.update_draft_report(
                report_uuid=request.report_uuid,
                updates=updates,
                actor=request.actor,
                reason=request.reason,
            )
        except DraftReportMutationBlockedError as exc:
            return {
                "success": False,
                "error": "not_draft",
                "report_uuid": str(exc.report_uuid),
                "status": exc.status,
            }
        if report is None:
            return {
                "success": False,
                "error": "not_found",
                "report_uuid": str(request.report_uuid),
            }
        await db.commit()
        response = InvestmentReportResponse.model_validate(report)
    return {"success": True, "report": response.model_dump(mode="json", by_alias=True)}


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
    offset: int = 0,
) -> dict:
    # ROB-465: the MCP list returns lightweight summaries (uuid/title/status/
    # kst_date + filter context) instead of full report bodies — full reports
    # (market_snapshot/portfolio_snapshot/report_metadata) blew the response
    # token budget (~78k chars for ~20 reports). Detail lives in
    # investment_report_get. The HTTP router keeps the full
    # InvestmentReportListResponse contract (unchanged).
    capped = max(1, min(int(limit), 100))
    eff_offset = max(0, int(offset))
    async with AsyncSessionLocal() as db:
        service = InvestmentReportQueryService(db)
        # Fetch one extra row to derive has_more without a separate count query.
        rows = await service.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            limit=capped + 1,
            offset=eff_offset,
        )
        has_more = len(rows) > capped
        page = rows[:capped]
        summaries = [_report_summary(r) for r in page]
    next_offset = eff_offset + len(page) if has_more else None
    return {
        "success": True,
        "reports": summaries,
        "pagination": {
            "returned_count": len(summaries),
            "offset": eff_offset,
            "limit": capped,
            "has_more": has_more,
            "next_offset": next_offset,
        },
    }


def _report_summary(row: Any) -> dict[str, Any]:
    """ROB-465: lightweight list row — identifiers + filter context, no bodies.

    Read straight off the ORM row (``InvestmentReport``); ``kst_date`` is not a
    column, so recover it from the idempotency key.
    """
    return {
        "report_uuid": str(row.report_uuid),
        "report_type": row.report_type,
        "market": row.market,
        "market_session": row.market_session,
        "account_scope": row.account_scope,
        "title": row.title,
        "status": row.status,
        "kst_date": kst_date_from_report_key(row.idempotency_key),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


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
    watch_condition: dict | None = None,
    valid_until: str | None = None,
    attach_recommendation: bool = False,
) -> dict:
    request = ActivateWatchRequest.model_validate(
        {
            "item_uuid": item_uuid,
            "actor": actor,
            "idempotency_key": idempotency_key,
            "watch_condition": watch_condition,
            "valid_until": valid_until,
            "attach_recommendation": attach_recommendation,
        }
    )
    async with AsyncSessionLocal() as db:
        activation_svc = WatchActivationService(db)
        repo = InvestmentReportsRepository(db)
        alert_row = await activation_svc.activate(request)
        item_row = await repo.get_item_by_uuid(request.item_uuid)

        recommendation_attached: bool | None = None
        recommendation_attach_error: str | None = None

        if request.attach_recommendation and item_row is not None:
            gate_error = _watch_recommendation_verdict_error(
                item_row, action="attach_recommendation"
            )
            if gate_error is not None:
                recommendation_attached = False
                recommendation_attach_error = gate_error
            elif item_row.watch_recommendation:
                recommendation_attached = True
            elif item_row.symbol is None:
                recommendation_attached = False
                recommendation_attach_error = "item symbol missing"
            else:
                try:
                    rec_json = await _compute_watch_recommendation_json(
                        symbol=item_row.symbol,
                        market=alert_row.market,
                        valid_until=item_row.valid_until,
                    )
                    if rec_json.get("data_state") == "data_gap":
                        raise ValueError("refusing to attach a data_gap recommendation")
                    await repo.update_item_watch_recommendation(item_row.id, rec_json)
                    await db.flush()
                    item_row = await repo.get_item_by_uuid(request.item_uuid)
                    recommendation_attached = True
                except Exception as exc:  # noqa: BLE001 - attach is opt-in and fail-open
                    recommendation_attached = False
                    recommendation_attach_error = str(exc)

        await db.commit()

        response = InvestmentReportActivateWatchResponse(
            alert=InvestmentWatchAlertResponse.model_validate(alert_row),
            item=InvestmentReportItemResponse.model_validate(item_row),
            recommendation_attached=recommendation_attached,
            recommendation_attach_error=recommendation_attach_error,
        )
    return response.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# investment_watch_recommend (ROB-337 Slice 1)
# ---------------------------------------------------------------------------
_RECOMMEND_VERDICTS = {"watch_only", "limit_wait"}
_MARKET_MAP = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


def _watch_recommendation_verdict_error(item: Any, *, action: str) -> str | None:
    verdict = None
    evidence_snapshot = getattr(item, "evidence_snapshot", None)
    if isinstance(evidence_snapshot, dict):
        verdict = evidence_snapshot.get("action_verdict")
    if verdict not in _RECOMMEND_VERDICTS:
        return (
            f"{action} requires item action_verdict in "
            f"{{watch_only, limit_wait}}; got {verdict!r}"
        )
    return None


def _normalize_recommend_symbol(symbol: str, market: str) -> str:
    s = str(symbol or "").strip()
    if market == "crypto":
        up = s.upper()
        return up if "-" in up else f"KRW-{up}"
    if market == "us":
        return s.upper()
    return s


async def _compute_watch_recommendation_json(
    *,
    symbol: str,
    market: str,
    valid_until: datetime | None,
) -> dict[str, Any]:
    if market not in _MARKET_MAP:
        raise ValueError(f"unsupported_market: {market}")

    md_symbol = _normalize_recommend_symbol(symbol, market)
    md_market = _MARKET_MAP[market]
    quote = await market_data_service.get_quote(symbol=md_symbol, market=md_market)
    reference_price = (
        Decimal(str(quote.price)) if getattr(quote, "price", None) is not None else None
    )
    candles = await market_data_service.get_ohlcv(
        symbol=md_symbol,
        market=md_market,
        period="day",
        count=LOOKBACK_DAYS + ATR_PERIOD + 6,
    )
    ordered = sorted(candles, key=lambda c: c.timestamp)
    payload = compute_watch_recommendation(
        WatchPolicyInput(
            reference_price=reference_price,
            best_bid=None,
            best_ask=None,
            daily_highs=[Decimal(str(c.high)) for c in ordered],
            daily_lows=[Decimal(str(c.low)) for c in ordered],
            daily_closes=[Decimal(str(c.close)) for c in ordered],
        ),
        computed_at=datetime.now(UTC),
        valid_until=valid_until,
    )
    return payload.model_dump(mode="json")


async def investment_watch_recommend_impl(
    symbol: str,
    market: str,
    item_uuid: str | None = None,
    commit: bool = False,
    actor: str | None = None,
) -> dict:
    """ROB-337 — compute advisory buy-review price thresholds for a watch.

    Read-only by default (commit=False). Advisory only: NO order is created
    or submitted. commit=True persists onto the item's watch_recommendation
    column, gated on action_verdict in {watch_only, limit_wait} and a
    non-data_gap result.
    """
    if market not in _MARKET_MAP:
        return {"success": False, "error": "unsupported_market", "market": market}

    valid_until = None
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        item = None
        if item_uuid is not None:
            item = await repo.get_item_by_uuid(UUID(item_uuid))
            if item is not None:
                valid_until = item.valid_until

        rec_json = await _compute_watch_recommendation_json(
            symbol=symbol,
            market=market,
            valid_until=valid_until,
        )

        if not commit:
            return {"success": True, "committed": False, "recommendation": rec_json}

        if item_uuid is None or item is None:
            raise ValueError("commit=True requires an existing item_uuid")
        gate_error = _watch_recommendation_verdict_error(item, action="commit")
        if gate_error is not None:
            raise ValueError(gate_error)
        if rec_json.get("data_state") == "data_gap":
            raise ValueError("refusing to commit a data_gap recommendation")

        await repo.update_item_watch_recommendation(item.id, rec_json)
        await db.commit()
        return {
            "success": True,
            "committed": True,
            "item_uuid": item_uuid,
            "recommendation": rec_json,
        }


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
# investment_report_delta_get (ROB-376)
# ---------------------------------------------------------------------------
async def investment_report_delta_get_impl(
    report_uuid: str,
    near_pct: float = 1.0,
    account_type: str = "live",
    use_previous_as_baseline: bool = False,
) -> dict:
    from app.core.timezone import now_kst
    from app.services.investment_reports.delta_service import DeltaService

    try:
        parsed = UUID(report_uuid)
    except (ValueError, AttributeError, TypeError):
        return {"success": False, "error": "invalid_report_uuid"}

    async with AsyncSessionLocal() as db:
        # ROB-455 — make previous_report_uuid load-bearing as the delta baseline:
        # resolve to the report's predecessor when asked (falls back to the report
        # itself when the chain link is unset).
        if use_previous_as_baseline:
            repo = InvestmentReportsRepository(db)
            report = await repo.get_report_by_uuid(parsed)
            if report is not None and report.previous_report_uuid is not None:
                parsed = report.previous_report_uuid

        service = DeltaService(db)
        return await service.compute_delta(
            parsed,
            near_pct=near_pct,
            account_type=account_type,
            computed_at_kst=now_kst().isoformat(),
        )


# ---------------------------------------------------------------------------
# investment_report_set_status (ROB-455)
# ---------------------------------------------------------------------------
async def investment_report_set_status_impl(
    report_uuid: str,
    status: str,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    try:
        request = SetReportStatusRequest.model_validate(
            {
                "report_uuid": report_uuid,
                "status": status,
                "reason": reason,
                "actor": actor,
            }
        )
    except ValidationError as exc:
        return {"success": False, "error": "invalid_request", "detail": str(exc)}

    async with AsyncSessionLocal() as db:
        service = InvestmentReportIngestionService(db)
        report = await service.set_report_status(
            report_uuid=request.report_uuid,
            status=request.status,
            reason=request.reason,
            actor=request.actor,
        )
        if report is None:
            return {
                "success": False,
                "error": "not_found",
                "report_uuid": str(request.report_uuid),
            }
        await db.commit()
        response = InvestmentReportResponse.model_validate(report)
    return {
        "success": True,
        "status": request.status,
        **response.model_dump(mode="json", by_alias=True),
    }


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
    budget_basis: str = "available_usd",
    operator_budget_override_usd: float | None = None,
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

    validated_items, item_error = _validate_report_items(items)
    if item_error is not None:
        return item_error

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
        "budget_basis": budget_basis,
        "operator_budget_override_usd": operator_budget_override_usd,
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
# investment_watch_events_list_recent (ROB-602 Task 3)
# ---------------------------------------------------------------------------
async def investment_watch_events_list_recent_impl(
    market: str | None = None,
    since_timestamp: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """최근 DELIVERED watch 트리거 이벤트 조회(운영자 poller/수동용, read-only).

    delivery_status='delivered' 이벤트만, delivered_at>=since_timestamp, delivered_at 오름차순.
    디듀프는 event_uuid. 브로커/주문/감시 mutation 없음.
    """
    parsed_since = None
    if since_timestamp:
        try:
            parsed_since = datetime.fromisoformat(
                since_timestamp.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            return {
                "success": False,
                "error": "invalid_timestamp",
                "hint": "ISO8601, e.g. 2026-06-20T12:34:56Z",
            }
    capped = max(1, min(int(limit), 500))
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        events = await repo.list_events_by_delivery_status(
            delivery_status="delivered",
            delivered_since=parsed_since,
            market=market,
            limit=capped,
        )
    return {
        "success": True,
        "count": len(events),
        "events": [
            InvestmentWatchEventResponse.model_validate(e).model_dump(
                mode="json", by_alias=True
            )
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_investment_report_tools(
    mcp: FastMCP,
    *,
    include_snapshot_generator: bool = True,
) -> None:
    mcp.tool(
        name="investment_report_create",
        description=CREATE_DESCRIPTION,
    )(investment_report_create_impl)
    mcp.tool(
        name="investment_report_add_items",
        description=ADD_ITEMS_DESCRIPTION,
    )(investment_report_add_items_impl)
    mcp.tool(
        name="investment_report_update",
        description=UPDATE_DESCRIPTION,
    )(investment_report_update_impl)
    mcp.tool(
        name="investment_report_list",
        description=(
            "List investment_reports filtered by market / market_session / "
            "account_scope / status / report_type. Returns lightweight summaries "
            "(report_uuid/title/status/kst_date + filter context) — NOT full "
            "report bodies; fetch detail via investment_report_get. limit clamped "
            "to 1..100 (default 20); paginate with offset using "
            "pagination.next_offset (null when no more)."
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
            "Record an operator decision on one investment_report_item. Verbs: "
            "approve | deny | defer | skip | partial_approve | cancel | reprice "
            "(ROB-455 order-lifecycle verbs: cancel = withdraw a tranche, "
            "reprice = adjust levels). Idempotent per (item_uuid, verb, actor) by "
            "default; pass idempotency_key to override. partial_approve and "
            "reprice both require a non-empty approved_payload_snapshot (the "
            "scoped/adjusted params). item.status projection: approve/reprice -> "
            "approved, deny/cancel -> denied, defer -> deferred, skip -> "
            "unchanged (the exact verb is preserved in the decision audit row)."
        ),
    )(investment_report_decide_item_impl)
    mcp.tool(
        name="investment_report_activate_watch",
        description=(
            "Activate an approved watch item into investment_watch_alerts "
            "as an immutable activation snapshot. Idempotent per source item. "
            "For operation='review' watches created without a condition, pass "
            "watch_condition (metric/operator/threshold) and valid_until to arm "
            "them; activating such a watch without a condition fails with an "
            "actionable error rather than 'corrupt state'."
        ),
    )(investment_report_activate_watch_impl)
    mcp.tool(
        name="investment_report_context_get",
        description=CONTEXT_GET_DESCRIPTION,
    )(investment_report_context_get_impl)
    mcp.tool(
        name="investment_report_delta_get",
        description=(
            "Read-only intraday delta vs a baseline report. Given report_uuid "
            "(the open/prior report), returns three deterministic deltas for Hermes "
            "to compose: levels_delta (journal target/stop touch x live), "
            "holdings_pnl_delta (per-symbol live P/L vs the baseline P/L from the "
            "snapshot bundle, or the create-time portfolio_snapshot JSON when no "
            "bundle is present), and index_delta (live index vs the report's "
            "frozen market_snapshot baseline). Per-signal fail-open: a degraded "
            "signal is "
            "null with a reason under 'unavailable'; missing data is never coerced "
            "to zero. No broker/order/watch mutation."
        ),
    )(investment_report_delta_get_impl)
    if include_snapshot_generator:
        mcp.tool(
            name="investment_report_generate_from_bundle",
            description=GENERATE_FROM_BUNDLE_DESCRIPTION,
        )(investment_report_generate_from_bundle_impl)
    mcp.tool(
        name="investment_watch_recommend",
        description=(
            "ROB-337 — compute advisory buy-review price thresholds "
            "(entry_review_below_price, suggested_limit_price_range, "
            "max_chase_price, invalidation) for a symbol from deterministic "
            "market evidence. Read-only by default; commit=True persists onto "
            "an item's watch_recommendation (gated on action_verdict in "
            "{watch_only, limit_wait}, refused on data_gap). Advisory only — "
            "no order is created or submitted."
        ),
    )(investment_watch_recommend_impl)
    mcp.tool(
        name="investment_report_set_status",
        description=(
            "ROB-455 — transition a report's lifecycle status to superseded | "
            "decided | expired (draft/published are entry states set at create, "
            "not transition targets here). Idempotent: setting the current status "
            "is a no-op success. Records the transition (reason/actor) in "
            "report_metadata.status_transitions for traceability. Use this to "
            "mark a report explicitly superseded instead of relying on a "
            "created_at heuristic — note that chaining a new report via "
            "previous_report_uuid already auto-supersedes its predecessor. "
            "No broker / order / watch mutation."
        ),
    )(investment_report_set_status_impl)
    mcp.tool(
        name="investment_watch_events_list_recent",
        description=(
            "최근 DELIVERED watch 트리거 이벤트 목록(운영자 poller/수동 조회용). "
            "market 필터 + since_timestamp(ISO8601, delivered_at>=) + limit(1..500). "
            "delivered만 노출(skipped/failed 제외). 디듀프=event_uuid. "
            "Read-only. 브로커/주문/감시 mutation 없음."
        ),
    )(investment_watch_events_list_recent_impl)


__all__ = [
    "INVESTMENT_REPORT_TOOL_NAMES",
    "investment_report_activate_watch_impl",
    "investment_report_add_items_impl",
    "investment_report_context_get_impl",
    "investment_report_create_impl",
    "investment_report_decide_item_impl",
    "investment_report_delta_get_impl",
    "investment_report_generate_from_bundle_impl",
    "investment_report_get_impl",
    "investment_report_list_impl",
    "investment_report_set_status_impl",
    "investment_report_update_impl",
    "investment_watch_events_list_recent_impl",
    "investment_watch_recommend_impl",
    "register_investment_report_tools",
]

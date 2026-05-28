"""ROB-273 — Snapshot-backed advisory report generator.

Single-responsibility service that:

1. ensures (or reuses) a snapshot bundle for the request's
   ``market`` / ``account_scope`` via
   :class:`SnapshotBundleEnsureService`;
2. derives the persisted snapshot metadata
   (``overall`` freshness, coverage, unavailable_sources, conflicts);
3. pre-flights the publishing stale gate so a clearly-blocked report
   never reaches the DB (the ingestion service's own gate is still the
   authoritative layer — this one is a fast-fail);
4. normalises the user-provided items through :func:`to_jsonable` so any
   ``Decimal`` / ``datetime`` / ``UUID`` values are safe for JSONB; and
5. delegates the actual persistence to
   :class:`InvestmentReportIngestionService`.

The generator never mutates broker state, never activates watch alerts,
never registers a scheduler job, and never reaches outside the
configured collector registry. Optional collector failures degrade the
bundle to ``partial`` and are surfaced via ``unavailable_sources`` /
``warnings`` but never block.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import IngestReportRequest
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.bundle_aware_publishing import (
    BundleAwarePublishingResult,
    StaleGateRejection,
    evaluate_stale_gate_for_ingest,
)
from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
    EXTERNAL_AUDIT_KINDS,
)
from app.services.action_report.common.diagnostics import (
    build_report_diagnostics,
    classify_why_no_action,
)
from app.services.action_report.common.jsonable import to_jsonable
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.action_report.snapshot_backed.intraday_floor import (
    ensure_action_floor,
    is_intraday_action,
)
from app.services.action_report.snapshot_backed.proposal_classifier import (
    ClassifierContext,
    classify_items,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
    ReportGenerationResponse,
)
from app.services.action_report.snapshot_backed.symbol_derivation import (
    SymbolDerivation,
    SymbolDerivationService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_snapshots.collectors import SnapshotCollectorRegistry
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

_MARKET_ACCOUNT_PAIRS: dict[str, str] = {
    "kr": "kis_live",
    "us": "kis_live",  # ROB-297 — KIS overseas (US) stock account.
    "crypto": "upbit_live",
}

_BUNDLE_STATUS_TO_OVERALL: dict[str, str] = {
    "complete": "fresh",
    "partial": "partial",
    "stale_fallback": "hard_stale",
    "failed": "failed",
    # 'reused' falls through to the stored summary's own 'overall'.
}

# Bundle.status values that mean a published report cannot be written.
_BLOCKING_BUNDLE_STATUSES_FOR_PUBLISHED: frozenset[str] = frozenset(
    {"stale_fallback", "failed"}
)

# ROB-278 Phase 2 — worst-case rank used to derive ``overall`` from per-kind
# statuses when the stored summary is missing an explicit ``overall`` key
# (most common cause: bundle.status='reused' carrying an older summary that
# only stored per-kind entries). Higher rank = worse.
_KIND_STATUS_RANK: dict[str, int] = {
    "fresh": 0,
    "soft_stale": 1,
    "partial": 2,
    "hard_stale": 3,
    "failed": 4,
    "unavailable": 5,
}
_RANK_TO_KIND_STATUS: dict[int, str] = {v: k for k, v in _KIND_STATUS_RANK.items()}


def _derive_overall_from_kind_statuses(
    summary: Mapping[str, Any],
    *,
    exclude_kinds: frozenset[str] = frozenset(),
) -> str | None:
    """Return the worst per-kind status in ``summary``, or ``None`` if the
    summary carries no recognisable per-kind status entries.

    ``exclude_kinds`` (ROB-323) drops optional/external kinds so an
    operator-driven stub's ``unavailable`` cannot pollute the core overall.
    """
    worst_rank = -1
    for kind, info in summary.items():
        if kind == "overall" or kind in exclude_kinds or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        if not isinstance(status, str):
            continue
        rank = _KIND_STATUS_RANK.get(status)
        if rank is None:
            continue
        if rank > worst_rank:
            worst_rank = rank
    if worst_rank < 0:
        return None
    return _RANK_TO_KIND_STATUS[worst_rank]


def _optional_kind_names(coverage_summary: Any) -> frozenset[str]:
    """Kinds that must not pollute the derived core ``overall`` (ROB-323).

    Union of the coverage summary's ``optional`` bucket and the always-external
    audit kinds. Used only on the ``reused`` fallback path, where there is no
    direct bundle-status → overall mapping.
    """
    coverage = to_jsonable(coverage_summary) or {}
    names: set[str] = set(EXTERNAL_AUDIT_KINDS)
    if isinstance(coverage, Mapping):
        optional = coverage.get("optional")
        if isinstance(optional, Mapping):
            names.update(str(k) for k in optional)
    return frozenset(names)


class SnapshotBackedReportGeneratorError(RuntimeError):
    """Raised when the request itself cannot proceed (mismatched scope, etc.)."""


class PublishBlockedByStaleGateError(RuntimeError):
    """Raised when a ``status='published'`` request would be blocked.

    Carries both the bundle outcome and the gate result so the caller
    can surface either to the user.
    """

    def __init__(
        self,
        *,
        reason: str,
        bundle_status: str,
        freshness_summary: Mapping[str, Any],
        stale_gate: BundleAwarePublishingResult | None,
    ) -> None:
        super().__init__(reason)
        self.bundle_status = bundle_status
        self.freshness_summary = dict(freshness_summary)
        self.stale_gate = stale_gate


class SnapshotBackedReportGenerator:
    """Generate a snapshot-backed advisory report end to end."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        ensure_service: SnapshotBundleEnsureService | None = None,
        ingestion_service: InvestmentReportIngestionService | None = None,
        collector_registry: SnapshotCollectorRegistry | None = None,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
        symbol_derivation_service: SymbolDerivationService | None = None,
    ) -> None:
        self._session = session
        registry = collector_registry or production_collector_registry(session)
        self._ensure_service = ensure_service or SnapshotBundleEnsureService(
            session, collectors=registry
        )
        self._ingestion_service = ingestion_service or InvestmentReportIngestionService(
            session
        )
        self._snapshots_repo = snapshots_repository or InvestmentSnapshotsRepository(
            session
        )
        self._symbol_derivation = symbol_derivation_service or SymbolDerivationService(
            session
        )

    async def generate(
        self, request: ReportGenerationRequest
    ) -> ReportGenerationResponse:
        self._validate_pair(request)

        # ROB-352 — deterministic regeneration: by default, an existing report
        # for this idempotency key is returned FROM THE STORED ROW. We never
        # recompute a divergent, unstored payload on the default path (the old
        # behaviour built the response from a fresh computation while ingest()
        # silently returned the stale stored row). Only an explicit overwrite
        # recomputes and transactionally replaces.
        if not request.overwrite_existing:
            found = await self._ingestion_service.get_existing_with_item_count(request)
            if found is not None:
                existing, item_count = found
                return self._response_from_stored(existing, item_count, request)

        # ROB-278 — derive symbol scope (seed + portfolio/journal/watch/candidate)
        # before ensuring the bundle so the symbol collector sees the union.
        derivation = await self._symbol_derivation.derive(
            market=request.market,
            account_scope=request.account_scope,
            user_id=request.user_id,
            seed_symbols=request.symbols,
        )

        ensure_response = await self._ensure_service.ensure(
            EnsureBundleRequest(
                purpose="report_generation",
                market=request.market,
                account_scope=request.account_scope,
                policy_version=request.policy_version,
                mode="ensure_fresh",
                symbols=derivation.symbols or None,
                candidate_limit=request.candidate_limit,
                requested_by=request.requested_by,
                user_id=request.user_id,
            )
        )

        if ensure_response.bundle_uuid is None:
            # ensure_fresh should always materialise a bundle (even a failed
            # one); a None here means upstream broke its own contract.
            raise SnapshotBackedReportGeneratorError(
                "bundle ensure returned no bundle_uuid; "
                f"status={ensure_response.status!r}"
            )

        # ROB-287 — the in-process LLM composition branch (auto_compose=True)
        # was removed; LLM reasoning/composition is owned by Hermes via the
        # context-export + ingest contract. ``ReportGenerationRequest``
        # rejects ``auto_compose=True`` at validation time, so we always
        # take the deterministic path here.
        #
        # ROB-278 Phase 2 — deterministic evidence-driven auto-emit can
        # populate items from the snapshot bundle BEFORE the classifier
        # runs. The classifier then enforces operation/apply_policy +
        # quote-evidence gates on whatever the proposer produced. This
        # proposer remains a deterministic, explicit-flag-only path
        # and is never co-mingled with Hermes composition against the
        # same bundle.
        if request.auto_emit_from_evidence:
            proposed = await self._auto_emit_items_from_bundle(
                bundle_uuid=ensure_response.bundle_uuid,
                request=request,
            )
            request = request.model_copy(update={"items": [*request.items, *proposed]})

        # ROB-274 — classify draft items against persisted bundle state.
        # We read payloads back from the DB (via list_bundle_items_with_snapshots)
        # so the classifier sees the same data that was just persisted, and
        # downstream audits remain reproducible.
        classifier_context = await self._build_classifier_context(
            bundle_uuid=ensure_response.bundle_uuid,
            missing_sources=list(ensure_response.missing_sources),
        )
        request = request.model_copy(
            update={
                "items": classify_items(
                    items=list(request.items),
                    context=classifier_context,
                )
            }
        )

        coverage_summary = to_jsonable(ensure_response.coverage_summary)
        freshness_summary = self._enrich_freshness_summary(ensure_response)
        unavailable_sources = self._build_unavailable_sources(
            ensure_response.missing_sources, freshness_summary
        )
        source_conflicts: dict[str, Any] = {}

        # ROB-318 Phase 3 — deterministic report diagnostics, computed before the
        # ingest request so they persist on the report row (PR-B). why_no_action
        # tells a genuine hold from a data-blocked / stale-gated one; the bundle
        # also carries data_sufficiency_by_source + report_quality_summary. All
        # deterministic — Hermes composes the prose. Action items are item_kind
        # 'action' (watch/risk are not buy/sell actions).
        why_no_action = classify_why_no_action(
            freshness_summary=freshness_summary,
            bundle_status=ensure_response.status,
            has_action_items=any(
                getattr(it, "item_kind", None) == "action" for it in request.items
            ),
        )

        # ROB-335 — intraday non-empty floor: never let an intraday_action
        # report succeed with items=[]; synthesize an explicit no-action /
        # data-gap item from the deterministic why_no_action verdict.
        if is_intraday_action(request.policy_version):
            request = request.model_copy(
                update={
                    "items": ensure_action_floor(
                        list(request.items), why_no_action=why_no_action
                    )
                }
            )

        report_diagnostics = build_report_diagnostics(
            freshness_summary=freshness_summary,
            bundle_status=ensure_response.status,
            why_no_action=why_no_action,
            snapshot_bundle_uuid=str(ensure_response.bundle_uuid),
        )

        if request.status == "published":
            self._guard_published(
                bundle_status=ensure_response.status,
                freshness_summary=freshness_summary,
            )

        ingest_request = self._build_ingest_request(
            request=request,
            bundle_uuid=ensure_response.bundle_uuid,
            coverage_summary=coverage_summary,
            freshness_summary=freshness_summary,
            unavailable_sources=unavailable_sources,
            source_conflicts=source_conflicts,
            report_diagnostics=report_diagnostics,
            symbol_derivation=derivation,
        )

        # Authoritative gate runs inside ingest(); this pre-flight is a
        # belt-and-braces check so a clearly-blocked published request
        # short-circuits with a friendlier exception type.
        gate_result = evaluate_stale_gate_for_ingest(ingest_request)
        if request.status == "published" and gate_result.reject:
            raise PublishBlockedByStaleGateError(
                reason=str(StaleGateRejection(gate_result)),
                bundle_status=ensure_response.status,
                freshness_summary=freshness_summary,
                stale_gate=gate_result,
            )

        report, reused, item_count = await self._ingestion_service.ingest_with_outcome(
            ingest_request,
            overwrite=request.overwrite_existing,
            overwrite_reason=request.overwrite_reason,
        )

        # ROB-352 — race guard: if a concurrent insert landed between the
        # precheck above and ingest(), ingest() returns the stored row
        # unchanged (reused=True). Reflect the STORED row so the response can
        # never disagree with what's persisted.
        if reused:
            return self._response_from_stored(report, item_count, request)

        return ReportGenerationResponse(
            report_uuid=report.report_uuid,
            snapshot_bundle_uuid=ensure_response.bundle_uuid,
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary=coverage_summary,
            snapshot_freshness_summary=freshness_summary,
            source_conflicts=source_conflicts,
            unavailable_sources=unavailable_sources,
            items_count=item_count,
            warnings=list(ensure_response.warnings),
            bundle_status=ensure_response.status,
            bundle_reused=not ensure_response.created,
            stale_gate=gate_result.to_metadata_summary(),
            why_no_action=why_no_action,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _validate_pair(self, request: ReportGenerationRequest) -> None:
        expected = _MARKET_ACCOUNT_PAIRS.get(request.market)
        if expected is None or request.account_scope != expected:
            raise SnapshotBackedReportGeneratorError(
                f"unsupported market/account_scope pair: "
                f"{request.market!r}/{request.account_scope!r}"
            )

    def _response_from_stored(
        self,
        report: Any,
        item_count: int,
        request: ReportGenerationRequest,
    ) -> ReportGenerationResponse:
        """ROB-352 — build a response that mirrors the persisted row.

        The default (non-overwrite) path returns this instead of recomputing,
        so the stored row and the returned payload can never disagree on the
        actionable/deferred/risk item set.
        """
        if report.snapshot_bundle_uuid is None:
            # Only this generator's rows share the key (report_type +
            # generator_version are in the key) and they always set a bundle
            # uuid. A None here means a foreign row collided — fail loudly
            # rather than fabricate a response.
            raise SnapshotBackedReportGeneratorError(
                "cannot reuse stored report: snapshot_bundle_uuid is missing; "
                "pass overwrite_existing=true with overwrite_reason to regenerate"
            )
        freshness = report.snapshot_freshness_summary or {}
        metadata = report.report_metadata or {}
        diagnostics = report.snapshot_report_diagnostics or {}
        return ReportGenerationResponse(
            report_uuid=report.report_uuid,
            snapshot_bundle_uuid=report.snapshot_bundle_uuid,
            snapshot_policy_version=report.snapshot_policy_version
            or request.policy_version,
            snapshot_coverage_summary=report.snapshot_coverage_summary or {},
            snapshot_freshness_summary=freshness,
            source_conflicts=report.source_conflicts or {},
            unavailable_sources=report.unavailable_sources or {},
            items_count=item_count,
            warnings=[
                "reused_existing_report: pass overwrite_existing=true with "
                "overwrite_reason to regenerate"
            ],
            # ROB-352 — distinct token: don't mix the bundle-ensure status
            # vocabulary (complete/partial/...) with the freshness ``overall``
            # token (fresh/partial/...). On reuse we didn't run ensure, so the
            # honest status is "reused"; freshness detail lives in
            # snapshot_freshness_summary.
            bundle_status="reused",
            bundle_reused=True,
            stale_gate=metadata.get("stale_gate", {}),
            why_no_action=diagnostics.get("why_no_action"),
            reused_existing=True,
        )

    async def _auto_emit_items_from_bundle(
        self,
        *,
        bundle_uuid: UUID,
        request: ReportGenerationRequest,
    ) -> list[Any]:
        """ROB-278 Phase 2 — deterministic, evidence-driven proposer.

        Reads the persisted snapshot bundle (portfolio / symbol+quote /
        candidate_universe / news / watch_context / journal) and emits
        ``IngestReportItem``s with ``operation="review"`` +
        ``apply_policy="requires_user_approval"``. The proposer is
        fail-closed: no candidates are emitted unless the evidence
        explicitly supports them (quote.status=='ok' with spread/depth for
        buy; portfolio.primary_source=='kis' + sellable_quantity > 0 for
        sell). Mutation paths are unreachable from this code path.
        """
        from app.services.action_report.snapshot_backed.auto_emit import (
            EvidenceAutoEmitter,
        )

        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            return []
        item_snapshot_pairs = (
            await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        )
        max_buy_candidates = (
            request.candidate_limit if request.candidate_limit is not None else 10
        )
        emitter = EvidenceAutoEmitter(
            max_buy_candidates=max_buy_candidates,
            intraday_floor=is_intraday_action(request.policy_version),
        )
        return emitter.propose(
            snapshots=[s for _i, s in item_snapshot_pairs],
            request_market=request.market,
            account_scope=request.account_scope,
        )

    async def _build_classifier_context(
        self,
        *,
        bundle_uuid: UUID,
        missing_sources: list[str],
    ) -> ClassifierContext:
        """Read back watch_context + pending_orders payloads from the bundle.

        ``pending_orders`` is OPTIONAL in the policy — if it's listed in
        ``missing_sources`` we surface ``None`` to the classifier so it
        downgrades dependent action items to ``operation='review'`` with
        a "확인 불가" rationale (per ROB-274 locked decision §3).

        ``pending_orders_seen`` distinguishes "snapshot kind appeared in
        bundle but was empty" (treated as ``[]``) vs "snapshot kind didn't
        appear at all" (treated as ``None``). Empty-but-present means the
        broker successfully reported no open orders. Absent means ensure()
        couldn't collect.
        """

        bundle = await self._snapshots_repo.get_bundle_by_uuid(bundle_uuid)
        if bundle is None:
            return ClassifierContext(
                active_watches=[],
                pending_orders=None if "pending_orders" in missing_sources else [],
            )

        item_snapshot_pairs = (
            await self._snapshots_repo.list_bundle_items_with_snapshots(bundle.id)
        )
        active_watches: list[dict[str, Any]] = []
        pending_orders: list[dict[str, Any]] = []
        pending_orders_seen = False
        symbol_quotes: dict[str, dict[str, Any]] = {}

        for _item, snapshot in item_snapshot_pairs:
            payload = snapshot.payload_json or {}
            if snapshot.snapshot_kind == "watch_context":
                # watch_context payload schema: {"active_alerts": [...], ...}
                alerts = payload.get("active_alerts") or []
                if isinstance(alerts, list):
                    active_watches.extend(alerts)
            elif snapshot.snapshot_kind == "pending_orders":
                pending_orders_seen = True
                # pending_orders payload schema: {"pending_orders": [...], ...}
                orders = payload.get("pending_orders") or []
                if isinstance(orders, list):
                    pending_orders.extend(orders)
            elif snapshot.snapshot_kind == "symbol":
                # ROB-278 Phase 2 — per-symbol quote evidence (when the
                # symbol collector enriched the snapshot via KIS read-only
                # quote/orderbook). symbol may be set on the snapshot row
                # itself (per-symbol kind), and is also echoed in payload.
                symbol = getattr(snapshot, "symbol", None) or payload.get("symbol")
                quote = payload.get("quote")
                if isinstance(symbol, str) and isinstance(quote, dict):
                    symbol_quotes[symbol] = quote

        # Honest "unavailable" signal: pending_orders kind was attempted but
        # didn't produce a usable result (or missing entirely from the bundle).
        unavailable = ("pending_orders" in missing_sources) or (not pending_orders_seen)
        return ClassifierContext(
            active_watches=active_watches,
            pending_orders=None if unavailable else pending_orders,
            symbol_quotes=symbol_quotes,
        )

    def _enrich_freshness_summary(self, ensure_response: Any) -> dict[str, Any]:
        summary = to_jsonable(ensure_response.freshness_summary) or {}
        if not isinstance(summary, dict):  # defensive
            summary = {"raw": summary}
        overall = summary.get("overall")
        if not isinstance(overall, str):
            # ROB-323 — prefer the authoritative, already core-aware bundle
            # status. snapshot_bundle._derive_bundle_status escalates only from
            # the 'required' coverage bucket, so optional/external kinds never
            # push it to stale_fallback/failed. Only fall back to a per-kind
            # scan when the status has no direct overall mapping (e.g.
            # 'reused'), and even then exclude optional/external kinds so an
            # operator-driven stub's 'unavailable' cannot pollute core overall.
            mapped = _BUNDLE_STATUS_TO_OVERALL.get(ensure_response.status)
            if mapped is not None:
                overall = mapped
            else:
                derived = _derive_overall_from_kind_statuses(
                    summary,
                    exclude_kinds=_optional_kind_names(
                        getattr(ensure_response, "coverage_summary", None)
                    ),
                )
                overall = (
                    derived
                    if derived is not None
                    else _BUNDLE_STATUS_TO_OVERALL.get(
                        ensure_response.status, "unavailable"
                    )
                )
            summary["overall"] = overall
        return summary

    def _build_unavailable_sources(
        self,
        missing_sources: list[str],
        freshness_summary: dict[str, Any],
    ) -> dict[str, Any]:
        sources: dict[str, Any] = {}
        for kind in missing_sources:
            sources[kind] = {"status": "unavailable"}
        for kind, info in freshness_summary.items():
            if kind == "overall" or not isinstance(info, Mapping):
                continue
            if info.get("status") in ("unavailable", "hard_stale", "failed"):
                sources.setdefault(kind, dict(info))
        return sources

    def _guard_published(
        self,
        *,
        bundle_status: str,
        freshness_summary: dict[str, Any],
    ) -> None:
        if bundle_status in _BLOCKING_BUNDLE_STATUSES_FOR_PUBLISHED:
            raise PublishBlockedByStaleGateError(
                reason=(
                    f"published report blocked: bundle_status={bundle_status!r} "
                    "is incompatible with published advisory reports"
                ),
                bundle_status=bundle_status,
                freshness_summary=freshness_summary,
                stale_gate=None,
            )
        for kind in CRITICAL_SNAPSHOT_KINDS:
            info = freshness_summary.get(kind)
            if not isinstance(info, Mapping):
                continue
            status = info.get("status")
            if status in CRITICAL_KIND_DEGRADING_STATUSES:
                raise PublishBlockedByStaleGateError(
                    reason=(
                        f"published report blocked: critical kind {kind!r} "
                        f"has status={status!r}"
                    ),
                    bundle_status=bundle_status,
                    freshness_summary=freshness_summary,
                    stale_gate=None,
                )

    def _build_ingest_request(
        self,
        *,
        request: ReportGenerationRequest,
        bundle_uuid: UUID,
        coverage_summary: dict[str, Any],
        freshness_summary: dict[str, Any],
        unavailable_sources: dict[str, Any],
        source_conflicts: dict[str, Any],
        report_diagnostics: dict[str, Any] | None = None,
        symbol_derivation: SymbolDerivation | None = None,
    ) -> IngestReportRequest:
        # Normalise items — each evidence_snapshot / trigger_checklist /
        # max_action / metadata is a free-form dict the caller filled in,
        # so they all go through to_jsonable() to be JSONB-safe.
        normalized_items = []
        for item in request.items:
            item_dict = item.model_dump(mode="python")
            for key in (
                "evidence_snapshot",
                "trigger_checklist",
                "max_action",
                "metadata",
                "target_ref",
                "current_state",
                "proposed_state",
                "diff",
            ):
                if key in item_dict and item_dict[key] is not None:
                    item_dict[key] = to_jsonable(item_dict[key])
            normalized_items.append(item_dict)

        metadata = to_jsonable(dict(request.metadata) or {})
        if not isinstance(metadata, dict):  # safety net
            metadata = {}
        metadata.setdefault("snapshot_backed_generator", True)
        metadata.setdefault(
            "generator_signature",
            {
                "report_type": request.report_type,
                "policy_version": request.policy_version,
                "generator_version": request.generator_version,
            },
        )
        if symbol_derivation is not None:
            metadata.setdefault(
                "symbol_derivation",
                to_jsonable(symbol_derivation.provenance),
            )

        return IngestReportRequest(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            created_by_profile=request.created_by_profile,
            title=request.title,
            summary=request.summary,
            risk_summary=request.risk_summary,
            thesis_text=request.thesis_text,
            no_action_note=request.no_action_note,
            market_snapshot={},
            portfolio_snapshot={},
            previous_report_uuid=request.previous_report_uuid,
            status=request.status,
            metadata=metadata,
            valid_until=request.valid_until,
            published_at=request.published_at,
            items=normalized_items,
            generator_version=request.generator_version,
            kst_date=request.kst_date,
            snapshot_bundle_uuid=bundle_uuid,
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary=coverage_summary,
            snapshot_freshness_summary=freshness_summary,
            source_conflicts=source_conflicts,
            unavailable_sources=unavailable_sources,
            snapshot_report_diagnostics=report_diagnostics,
        )

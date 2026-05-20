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
)
from app.services.action_report.common.jsonable import to_jsonable
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.action_report.snapshot_backed.proposal_classifier import (
    ClassifierContext,
    classify_items,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
    ReportGenerationResponse,
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

    async def generate(
        self, request: ReportGenerationRequest
    ) -> ReportGenerationResponse:
        self._validate_pair(request)

        ensure_response = await self._ensure_service.ensure(
            EnsureBundleRequest(
                purpose="report_generation",
                market=request.market,
                account_scope=request.account_scope,
                policy_version=request.policy_version,
                mode="ensure_fresh",
                symbols=request.symbols,
                candidate_limit=request.candidate_limit,
                requested_by=request.requested_by,
            )
        )

        if ensure_response.bundle_uuid is None:
            # ensure_fresh should always materialise a bundle (even a failed
            # one); a None here means upstream broke its own contract.
            raise SnapshotBackedReportGeneratorError(
                "bundle ensure returned no bundle_uuid; "
                f"status={ensure_response.status!r}"
            )

        if request.auto_compose:
            # ROB-279 — synthesize report via staged snapshot-backed pipeline.
            from types import SimpleNamespace

            from app.core.config import settings
            from app.services.ai_providers.gemini_provider import GeminiProvider
            from app.services.investment_stages.budget import StageLLMBudget
            from app.services.investment_stages.composer import FinalComposer
            from app.services.investment_stages.rate_limited_provider import (
                RateLimitedGeminiProvider,
            )
            from app.services.investment_stages.stage_runner import StageRunner
            from app.services.investment_stages.stages.registry import (
                get_default_v1_stages,
            )

            class _LocalBundleRead:
                def __init__(self, repo):
                    self._repo = repo

                async def get_bundle(self, *, bundle_uuid: UUID):
                    bundle = await self._repo.get_bundle_by_uuid(bundle_uuid)
                    if not bundle:
                        return None
                    items = await self._repo.list_bundle_items_with_snapshots(bundle.id)
                    return SimpleNamespace(bundle=bundle, items=[i[1] for i in items])

            budget = StageLLMBudget(max_calls=4)
            provider = RateLimitedGeminiProvider(
                GeminiProvider(api_key=settings.gemini_advisor_api_key or "")
            )
            stage_runner = StageRunner(
                session=self._session,
                bundle_read_service=_LocalBundleRead(self._snapshots_repo),
                stages=get_default_v1_stages(provider, budget),
            )
            stage_run = await stage_runner.run(
                snapshot_bundle_uuid=ensure_response.bundle_uuid,
                market=request.market,
                market_session=request.market_session,
                account_scope=request.account_scope,
            )

            composer = FinalComposer(provider, budget)
            composed_req = await composer.compose(
                run_uuid=stage_run.run_uuid,
                snapshot_bundle_uuid=ensure_response.bundle_uuid,
                market=request.market,
                market_session=request.market_session,
                account_scope=request.account_scope,
                kst_date=request.kst_date,
                artifacts=stage_run.artifacts,
            )

            # Re-classify composed items against operational state
            classifier_context = await self._build_classifier_context(
                bundle_uuid=ensure_response.bundle_uuid,
                missing_sources=list(ensure_response.missing_sources),
            )
            composed_items = classify_items(
                items=composed_req.items,
                context=classifier_context,
            )

            coverage_summary = to_jsonable(ensure_response.coverage_summary)
            freshness_summary = self._enrich_freshness_summary(ensure_response)
            unavailable_sources = self._build_unavailable_sources(
                ensure_response.missing_sources, freshness_summary
            )
            source_conflicts: dict[str, Any] = {}

            ingest_request = composed_req.model_copy(
                update={
                    "items": composed_items,
                    "snapshot_coverage_summary": coverage_summary,
                    "snapshot_freshness_summary": freshness_summary,
                    "unavailable_sources": unavailable_sources,
                    "source_conflicts": {},
                    "metadata": {
                        **composed_req.metadata,
                        "investment_stage_run_uuid": str(stage_run.run_uuid),
                        "snapshot_backed_generator": True,
                        "generator_signature": {
                            "report_type": composed_req.report_type,
                            "policy_version": request.policy_version,
                            "generator_version": "v2_staged",
                        },
                    },
                }
            )
        else:
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

        report = await self._ingestion_service.ingest(ingest_request)

        return ReportGenerationResponse(
            report_uuid=report.report_uuid,
            snapshot_bundle_uuid=ensure_response.bundle_uuid,
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary=coverage_summary,
            snapshot_freshness_summary=freshness_summary,
            source_conflicts=source_conflicts,
            unavailable_sources=unavailable_sources,
            items_count=len(ingest_request.items),
            warnings=list(ensure_response.warnings),
            bundle_status=ensure_response.status,
            bundle_reused=not ensure_response.created,
            stale_gate=gate_result.to_metadata_summary(),
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

        # Honest "unavailable" signal: pending_orders kind was attempted but
        # didn't produce a usable result (or missing entirely from the bundle).
        unavailable = ("pending_orders" in missing_sources) or (not pending_orders_seen)
        return ClassifierContext(
            active_watches=active_watches,
            pending_orders=None if unavailable else pending_orders,
        )

    def _enrich_freshness_summary(self, ensure_response: Any) -> dict[str, Any]:
        summary = to_jsonable(ensure_response.freshness_summary) or {}
        if not isinstance(summary, dict):  # defensive
            summary = {"raw": summary}
        overall = summary.get("overall")
        if not isinstance(overall, str):
            overall = _BUNDLE_STATUS_TO_OVERALL.get(
                ensure_response.status, "unavailable"
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

        return IngestReportRequest(
            report_type=request.report_type,
            market=request.market,
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
        )

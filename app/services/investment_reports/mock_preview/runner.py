"""ROB-373 — mock_preview report runner (Unit 2).

Projects a kis_live advisory report's items into a kis_mock / mock_preview report,
reusing account-independent evidence (via the shared NULL-scope snapshot rows and
carried-over cited_snapshot_uuids) and attaching a KIS-mock preview to each BUY
action item. Writes through InvestmentReportIngestionService ONLY — the
snapshot-backed generator's live-only guard is never touched.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    TargetRefPayload,
    WatchConditionPayload,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.mock_preview.bridge import (
    MockPreviewBridge,
    extract_order_params,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_MOCK_GENERATOR_VERSION = "v2-mock-preview"


class MockPreviewSourceMissing(Exception):
    """Raised when the live source report is absent or empty (fail-closed)."""


class MockPreviewReportRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        bridge: MockPreviewBridge | None = None,
        ensure_service: SnapshotBundleEnsureService | None = None,
    ) -> None:
        self._session = session
        self._reports_repo = InvestmentReportsRepository(session)
        self._snap_repo = InvestmentSnapshotsRepository(session)
        self._ingestion = InvestmentReportIngestionService(session)
        self._bridge = bridge if bridge is not None else MockPreviewBridge()
        # ROB-379 smoke finding: the default SnapshotBundleEnsureService registry
        # is EMPTY (Phase-2 stub), so an un-injected ensure produced a `failed`
        # kis_mock bundle that collected nothing — defeating evidence reuse. Wire
        # the production collector registry (read-only adapters) like the
        # report-generation entrypoints do, so the kis_mock ensure actually
        # collects (and dedups the shared NULL-scope evidence rows).
        self._ensure = (
            ensure_service
            if ensure_service is not None
            else SnapshotBundleEnsureService(
                session, collectors=production_collector_registry(session)
            )
        )

    async def run(
        self,
        *,
        live_report_uuid: UUID,
        market: str,
        market_session: str | None,
        policy_version: str,
        kst_date: str,
        created_by_profile: str,
        user_id: int | None = None,
    ) -> tuple[InvestmentReport, bool, int]:
        live = await self._reports_repo.get_report_by_uuid(live_report_uuid)
        if live is None:
            raise MockPreviewSourceMissing(f"live report not found: {live_report_uuid}")
        live_items = await self._reports_repo.list_items_for_report(live.id)
        if not live_items:
            raise MockPreviewSourceMissing(
                f"live report has no items: {live_report_uuid}"
            )

        # ROB-380 follow-up — the mock report is idempotent on this identity
        # (report_type/market/session/scope/execution_mode/kst_date/generator).
        # If a mock report already exists AND already cites the live bundle's
        # account-independent rows, it is already correct: return it unchanged
        # and build NO bundle. Building one would only be orphaned, because
        # ingest_with_outcome() would idempotently return this same row.
        existing_mock = await self._ingestion.find_existing_report(
            report_type=live.report_type,
            market=market,
            market_session=market_session,
            account_scope="kis_mock",
            execution_mode="mock_preview",
            kst_date=kst_date,
            generator_version=_MOCK_GENERATOR_VERSION,
        )
        if (
            existing_mock is not None
            and live.snapshot_bundle_uuid is not None
            and await self._shares_account_independent_rows(
                existing_mock.snapshot_bundle_uuid, live.snapshot_bundle_uuid
            )
        ):
            items = await self._reports_repo.list_items_for_report(existing_mock.id)
            return existing_mock, True, len(items)

        # ROB-380 — reuse the live bundle's account-independent (NULL-scope)
        # snapshot rows instead of re-collecting them, so the live and mock
        # reports cite the SAME snapshot_uuid. Account-bound kinds are still
        # collected fresh for kis_mock. Fall back to independent collection only
        # when the live report has no bundle to reuse (legacy / pre-ROB-373 rows).
        if live.snapshot_bundle_uuid is not None:
            ensure_resp = await self._ensure.ensure_reusing_account_independent(
                EnsureBundleRequest(
                    purpose="mock_preview_report",
                    market=market,  # type: ignore[arg-type]
                    account_scope="kis_mock",
                    policy_version=policy_version,
                    mode="ensure_fresh",
                    requested_by="claude_code",
                    user_id=user_id,
                ),
                reuse_from_bundle_uuid=live.snapshot_bundle_uuid,
            )
        else:
            ensure_resp = await self._ensure.ensure(
                EnsureBundleRequest(
                    purpose="mock_preview_report",
                    market=market,  # type: ignore[arg-type]
                    account_scope="kis_mock",
                    policy_version=policy_version,
                    mode="ensure_fresh",
                    requested_by="claude_code",
                    user_id=user_id,
                )
            )

        projected: list[IngestReportItem] = []
        for item in live_items:
            projected.append(await self._project(item))

        request = IngestReportRequest(
            report_type=live.report_type,
            market=market,  # type: ignore[arg-type]
            market_session=market_session,  # type: ignore[arg-type]
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile=created_by_profile,
            title=f"[MOCK PREVIEW] {live.title}",
            summary=live.summary,
            risk_summary=live.risk_summary,
            thesis_text=live.thesis_text,
            no_action_note=live.no_action_note,
            status="draft",
            metadata={"mock_preview_of_report_uuid": str(live.report_uuid)},
            items=projected,
            generator_version=_MOCK_GENERATOR_VERSION,
            kst_date=kst_date,
            # ensure_fresh always returns a bundle_uuid (None only occurs for mode="reuse_only").
            snapshot_bundle_uuid=ensure_resp.bundle_uuid,
            snapshot_policy_version=policy_version,
        )
        report, reused, count = await self._ingestion.ingest_with_outcome(request)

        # ROB-380 follow-up — when ingestion idempotently returned a STALE
        # existing report (one persisted before this fix, still pointing at a
        # non-sharing bundle), its snapshot_bundle_uuid is NOT the reuse bundle
        # we just built. Re-point it (report_uuid / items stay stable) so the
        # persisted report shares the live bundle's account-independent rows and
        # the freshly built reuse bundle is not orphaned.
        if reused and report.snapshot_bundle_uuid != ensure_resp.bundle_uuid:
            await self._reports_repo.update_report(
                report.id, snapshot_bundle_uuid=ensure_resp.bundle_uuid
            )
            await self._session.refresh(report)

        return report, reused, count

    async def _shares_account_independent_rows(
        self, candidate_bundle_uuid: UUID | None, live_bundle_uuid: UUID
    ) -> bool:
        """True when ``candidate`` already links EXACTLY the live bundle's
        account-independent snapshot rows (so rebuilding/re-pointing is a no-op).

        An empty live independent set returns False (nothing to share — fall
        through to the normal build path).
        """
        if candidate_bundle_uuid is None:
            return False
        live_independent = {
            s.snapshot_uuid
            for s in await self._snap_repo.list_account_independent_bundle_snapshots(
                live_bundle_uuid
            )
        }
        if not live_independent:
            return False
        candidate_independent = {
            s.snapshot_uuid
            for s in await self._snap_repo.list_account_independent_bundle_snapshots(
                candidate_bundle_uuid
            )
        }
        return candidate_independent == live_independent

    async def _project(self, item: InvestmentReportItem) -> IngestReportItem:
        evidence = dict(item.evidence_snapshot or {})
        max_action = dict(item.max_action or {})

        # BUY action items get a KIS-mock preview embedded into evidence.
        if item.item_kind == "action" and item.side == "buy":
            params = extract_order_params(
                symbol=item.symbol, evidence_snapshot=evidence, max_action=max_action
            )
            if params is None:
                evidence["mock_preview"] = {
                    "status": "skipped",
                    "reason": "insufficient_order_params",
                    "submit_enabled": False,
                }
            else:
                try:
                    evidence["mock_preview"] = await self._bridge.preview(params)
                except Exception as exc:  # noqa: BLE001 — isolate one item's failure
                    evidence["mock_preview"] = {
                        "status": "error",
                        "reason": type(exc).__name__,
                        "submit_enabled": False,
                    }

        watch_condition = (
            WatchConditionPayload.model_validate(item.watch_condition)
            if item.watch_condition
            else None
        )
        target_ref = (
            TargetRefPayload.model_validate(item.target_ref)
            if item.target_ref
            else None
        )

        return IngestReportItem(
            client_item_key=f"mockpv:{item.item_uuid}",
            item_kind=item.item_kind,  # type: ignore[arg-type]
            operation=item.operation,  # type: ignore[arg-type]
            symbol=item.symbol,
            side=item.side,  # type: ignore[arg-type]
            intent=item.intent,  # type: ignore[arg-type]
            target_kind=item.target_kind,  # type: ignore[arg-type]
            priority=item.priority,
            confidence=item.confidence,
            rationale=item.rationale,
            evidence_snapshot=evidence,
            watch_condition=watch_condition,
            trigger_checklist=list(item.trigger_checklist or []),
            max_action=max_action,
            valid_until=item.valid_until,
            metadata=dict(item.item_metadata or {}),
            target_ref=target_ref,
            current_state=item.current_state,
            proposed_state=item.proposed_state,
            diff=item.diff,
            apply_policy="requires_user_approval",
            decision_bucket=item.decision_bucket,
            cited_symbol_report_uuid=item.cited_symbol_report_uuid,
            cited_dimension_report_uuids=list(item.cited_dimension_report_uuids or []),
            cited_snapshot_uuids=list(item.cited_snapshot_uuids or []),
        )

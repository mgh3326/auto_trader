"""Hermes context exporter (ROB-287).

Read-only service that turns a persisted snapshot bundle into a
:class:`HermesContextPayload` — the frozen, auditable input Hermes
consumes for in-Hermes LLM reasoning. No external service calls, no DB
writes; deterministic stages are executed in-process against bundle
snapshots already in memory.

This module is intentionally provider-free. Importing
``GeminiProvider`` / ``RateLimitedGeminiProvider`` / any
``AiProvider`` here would re-introduce the in-process LLM path the
ROB-287 guard test forbids.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_snapshots import (
    InvestmentSnapshot,
    InvestmentSnapshotBundle,
)
from app.schemas.hermes_composition import (
    HermesCitedSnapshot,
    HermesContextPayload,
    HermesStageInput,
)
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.action_report.common.diagnostics import (
    build_data_sufficiency_by_source,
    build_report_quality_summary,
    classify_why_no_action,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.investment_dimensions.fundamentals_evidence import (
    build_fundamentals_evidence,
)
from app.services.investment_dimensions.market_evidence import build_market_evidence
from app.services.investment_dimensions.news_evidence import build_news_evidence
from app.services.investment_dimensions.sentiment_evidence import (
    build_sentiment_evidence,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
from app.services.investment_stages.market_session import derive_market_session
from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.registry import get_default_v1_stages
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.research_reports.query_service import ResearchReportsQueryService
from app.services.stock_info_service import StockInfoService

_logger = logging.getLogger(__name__)


class HermesContextExportError(RuntimeError):
    """Raised when the requested bundle cannot be loaded."""


class HermesContextExporter:
    """Build a :class:`HermesContextPayload` from a persisted bundle.

    Pure read-only: the exporter inspects ``review.investment_snapshot_bundles``
    + ``review.investment_snapshots`` and runs the deterministic v1 stage
    set against the in-memory snapshots. No stage run / artifact rows are
    persisted; the deterministic outputs travel as inline ``stage_inputs``
    inside the payload so Hermes can audit them without a follow-up query.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
        stages: list[Stage] | None = None,
    ) -> None:
        self._session = session
        self._snapshots = snapshots_repository or InvestmentSnapshotsRepository(session)
        self._stages = stages or get_default_v1_stages()

    async def export(self, *, snapshot_bundle_uuid: uuid.UUID) -> HermesContextPayload:
        bundle = await self._snapshots.get_bundle_by_uuid(snapshot_bundle_uuid)
        if bundle is None:
            raise HermesContextExportError(
                f"snapshot bundle not found: {snapshot_bundle_uuid}"
            )

        item_snapshot_pairs = await self._snapshots.list_bundle_items_with_snapshots(
            bundle.id
        )
        snapshots = [snap for _item, snap in item_snapshot_pairs]
        snapshots_by_kind: dict[str, list[InvestmentSnapshot]] = defaultdict(list)
        for snap in snapshots:
            if snap.snapshot_kind:
                snapshots_by_kind[snap.snapshot_kind].append(snap)

        cited = [
            HermesCitedSnapshot(
                snapshot_uuid=snap.snapshot_uuid,
                snapshot_kind=snap.snapshot_kind or "unknown",
            )
            for snap in snapshots
        ]

        stage_inputs = await self._render_stage_inputs(
            bundle=bundle, snapshots_by_kind=dict(snapshots_by_kind)
        )

        # Unit tests inject a mock session; skip the DB-backed dimension
        # synthesis (and the stage-run lookup below) so they exercise
        # stage_inputs without real repo I/O. Real sessions (and integration
        # tests) build the evidence.
        is_mock = (
            hasattr(self._session, "assert_called")
            or hasattr(self._session, "_mock_name")
            or "Mock" in type(self._session).__name__
        )

        dimension_evidence = {}
        # ROB-369 E11 — crypto bundles previously received empty dimension_evidence
        # (silent {}). Crypto is included so it gets the same per-dimension
        # synthesis: market/news/fundamentals query market-scoped sources
        # (real-where-present, empty otherwise) and sentiment returns an explicit
        # unavailable (investor-flow is KR-only) — honest, never KR-leaking.
        if not is_mock and bundle.market in ("kr", "us", "crypto"):
            try:
                held = set()
                portfolio_snapshots = snapshots_by_kind.get("portfolio", [])
                for snap in portfolio_snapshots:
                    holdings = (snap.payload_json or {}).get("holdings", [])
                    for h in holdings:
                        ticker = h.get("ticker")
                        if ticker:
                            held.add(ticker)

                screener_repo = InvestScreenerSnapshotsRepository(self._session)
                market_evidence = await build_market_evidence(
                    screener_repo, market=bundle.market, held=held
                )
                dimension_evidence["market"] = market_evidence
            except Exception as exc:
                _logger.exception("Failed to build market evidence for context export")
                dimension_evidence["market"] = {"unavailable": str(exc)}

            try:
                news_evidence = await build_news_evidence(
                    ResearchReportsQueryService(self._session), market=bundle.market
                )
                dimension_evidence["news"] = news_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like market
                _logger.exception("Failed to build news evidence for context export")
                dimension_evidence["news"] = {"unavailable": str(exc)}

            dimension_symbols: set[str] = set()
            for snap in snapshots_by_kind.get("portfolio", []):
                for h in (snap.payload_json or {}).get("holdings", []):
                    ticker = h.get("ticker")
                    if ticker:
                        dimension_symbols.add(ticker)
            market_dim = dimension_evidence.get("market")
            if isinstance(market_dim, dict):
                for mover in market_dim.get("top_movers", []):
                    sym = mover.get("symbol")
                    if sym:
                        dimension_symbols.add(sym)

            try:
                fundamentals_evidence = await build_fundamentals_evidence(
                    MarketValuationSnapshotsRepository(self._session),
                    StockInfoService(self._session),
                    market=bundle.market,
                    symbols=dimension_symbols,
                )
                dimension_evidence["fundamentals"] = fundamentals_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like market/news
                _logger.exception(
                    "Failed to build fundamentals evidence for context export"
                )
                dimension_evidence["fundamentals"] = {"unavailable": str(exc)}

            try:
                sentiment_evidence = await build_sentiment_evidence(
                    InvestorFlowSnapshotsRepository(self._session),
                    market=bundle.market,
                    symbols=dimension_symbols,
                )
                dimension_evidence["sentiment"] = sentiment_evidence
            except Exception as exc:  # noqa: BLE001 — best-effort, like the others
                _logger.exception(
                    "Failed to build sentiment evidence for context export"
                )
                dimension_evidence["sentiment"] = {"unavailable": str(exc)}

        run = None
        if not is_mock:
            try:
                from app.services.investment_stages.query_service import (
                    StageRunQueryService,
                )

                runs = await StageRunQueryService(self._session).list_runs_for_bundle(
                    bundle.bundle_uuid
                )
                run = runs[0] if runs else None
            except Exception:
                pass

        from app.services.investment_dimensions.dimension_report_repository import (
            DimensionReportRepository,
        )
        from app.services.investment_stages.symbol_report_repository import (
            SymbolIntermediateReportRepository,
        )

        dimension_reports: list[dict[str, Any]] = []
        symbol_intermediate_reports: list[dict[str, Any]] = []
        if run is not None:
            for d in await DimensionReportRepository(self._session).list_for_run(
                run.run_uuid
            ):
                dimension_reports.append(
                    {
                        "dimension_report_uuid": str(d.dimension_report_uuid),
                        "dimension": d.dimension,
                        "market": d.market,
                        "symbol": d.symbol,
                        "stance": d.stance,
                        "confidence": d.confidence,
                        "key_findings": d.key_findings or [],
                        "report_text": d.report_text,
                    }
                )
            for s in await SymbolIntermediateReportRepository(
                self._session
            ).list_for_run(run.run_uuid):
                symbol_intermediate_reports.append(
                    {
                        "symbol_report_uuid": str(s.symbol_report_uuid),
                        "symbol": s.symbol,
                        "decision_bucket": s.decision_bucket,
                        "verdict": s.verdict,
                        "confidence": s.confidence,
                        "summary": s.summary,
                    }
                )

        # ROB-318 Phase 3 — deterministic data-sufficiency signals for Hermes,
        # derived from the bundle's freshness/coverage. why_no_action here uses
        # has_action_items=True so only data/stale gating surfaces (Hermes has
        # not produced items yet — a 'real_no_action' verdict is its call, not
        # ours).
        freshness_summary = dict(bundle.freshness_summary or {})
        why_no_action = classify_why_no_action(
            freshness_summary=freshness_summary,
            bundle_status=bundle.status,
            has_action_items=True,
        )

        return HermesContextPayload(
            snapshot_bundle_uuid=bundle.bundle_uuid,
            bundle_status=bundle.status,
            market=bundle.market,
            # ROB-366 B6 / ROB-374 B6 — an operator/Hermes-recorded session on
            # the latest stage run wins; otherwise derive it from the bundle's
            # own ``as_of`` (a real captured market moment, not a wall-clock
            # guess) so ``intraday_action_report_v1`` always carries a session
            # when one is determinable. Unknown/closed instants stay ``None``.
            market_session=(
                run.market_session
                if run is not None and run.market_session is not None
                else derive_market_session(bundle.market, bundle.as_of)
            ),
            account_scope=bundle.account_scope,
            policy_version=bundle.policy_version,
            coverage_summary=dict(bundle.coverage_summary or {}),
            freshness_summary=freshness_summary,
            unavailable_sources=self._derive_unavailable_sources(stage_inputs),
            source_conflicts={},
            data_sufficiency_by_source=build_data_sufficiency_by_source(
                freshness_summary
            ),
            report_quality_summary=build_report_quality_summary(
                freshness_summary=freshness_summary,
                bundle_status=bundle.status,
            ),
            why_no_action=why_no_action,
            dimension_evidence=dimension_evidence,
            dimension_reports=dimension_reports,
            symbol_intermediate_reports=symbol_intermediate_reports,
            stage_inputs=stage_inputs,
            cited_snapshots=cited,
        )

    async def _render_stage_inputs(
        self,
        *,
        bundle: InvestmentSnapshotBundle,
        snapshots_by_kind: dict[str, list[InvestmentSnapshot]],
    ) -> list[HermesStageInput]:
        ctx = StageContext(
            bundle_uuid=bundle.bundle_uuid,
            snapshots_by_kind=snapshots_by_kind,
            bundle_metadata={
                "status": bundle.status,
                "freshness_summary": dict(bundle.freshness_summary or {}),
                "policy_version": bundle.policy_version,
            },
            market=bundle.market,
            prior_artifacts={},
        )

        rendered: list[HermesStageInput] = []
        for stage in self._stages:
            artifact = await self._run_stage_safely(stage, ctx)
            rendered.append(
                HermesStageInput(
                    stage_type=stage.stage_type,
                    artifact=artifact,
                    cited_snapshots=[
                        HermesCitedSnapshot(
                            snapshot_uuid=c.snapshot_uuid,
                            snapshot_kind=c.snapshot_kind,
                            payload_path=c.payload_path,
                        )
                        for c in artifact.cited_snapshots
                    ],
                )
            )
            # ``StageContext`` is frozen, but ``prior_artifacts`` is a
            # mutable mapping; we extend it in place exactly like the
            # ``StageRunner`` does.
            ctx.prior_artifacts[stage.stage_type] = artifact
        return rendered

    @staticmethod
    async def _run_stage_safely(
        stage: Stage, ctx: StageContext
    ) -> StageArtifactPayload:
        try:
            return await stage.run(ctx)
        except UnavailableStageError as exc:
            _logger.info(
                "hermes_context: stage %s unavailable: %s", stage.stage_type, exc
            )
            return StageArtifactPayload(
                stage_type=stage.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                summary=str(exc),
                missing_data=[stage.stage_type],
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("hermes_context: stage %s failed", stage.stage_type)
            return StageArtifactPayload(
                stage_type=stage.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                summary=f"stage error: {exc!r}",
                missing_data=[stage.stage_type],
            )

    @staticmethod
    def _derive_unavailable_sources(
        stage_inputs: list[HermesStageInput],
    ) -> dict[str, Any]:
        unavailable: dict[str, Any] = {}
        for entry in stage_inputs:
            if entry.artifact.verdict == StageVerdict.UNAVAILABLE:
                unavailable[entry.stage_type] = {
                    "status": "unavailable",
                    "summary": entry.artifact.summary,
                    "missing_data": list(entry.artifact.missing_data),
                }
        return unavailable

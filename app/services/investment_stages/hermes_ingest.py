"""Hermes ingest services (ROB-287).

Two Hermes-initiated write paths land here:

1. :class:`HermesStageArtifactsIngestService` — Hermes ingests one or more
   :class:`StageArtifactPayload` rows for a single ``run_uuid``. The run
   row is auto-created on the first ingest (Hermes-generated UUID per
   locked decision §D1) and reused on subsequent calls. Append-only
   idempotency: re-ingesting the same ``(run_uuid, stage_type)`` with
   identical content returns the stored row; differing content is
   rejected (§D3).
2. :class:`HermesCompositionIngestService` — Hermes ingests a final
   composition (title/summary/items) that becomes an
   ``InvestmentReport`` row via the existing
   :class:`InvestmentReportIngestionService`. When the composition's
   ``metadata.investment_stage_run_uuid`` matches an existing
   ``running`` stage run, the composition ingest also flips that run
   to ``completed`` (§D4 — composition is the canonical finalizer).

Hard invariants:

* No external LLM is called here.
* No broker / order / watch / order-intent mutation reachable.
* Stage-artifact ingest does NOT advance ``run.status`` to a terminal
  state. Composition ingest is the only path that closes a run.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport
from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun
from app.schemas.hermes_composition import (
    HERMES_COMPOSITION_VERSION,
    HermesCompositionIngestRequest,
    HermesStageArtifactsIngestRequest,
)
from app.schemas.investment_reports import (
    AccountScopeLiteral,
    IngestReportRequest,
    MarketLiteral,
    MarketSessionLiteral,
)
from app.schemas.investment_stages import StageArtifactPayload
from app.services.investment_dimensions.dimension_report_repository import (
    DimensionReportRepository,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.investment_report_news_service import (
    InvestmentReportNewsService,
)
from app.services.investment_reports.repository import (
    InvestmentReportsRepository,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
from app.services.investment_stages.repository import (
    AppendOnlyViolation,
    InvestmentStagesRepository,
)
from app.services.investment_stages.symbol_report_repository import (
    SymbolIntermediateReportRepository,
)

_logger = logging.getLogger(__name__)


def _project_market_snapshot(bundle_pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """ROB-470 — project a bundle ``market`` snapshot into the report's
    ``market_snapshot`` column shape the delta loader expects.

    The bundle market payload stores indices at top level (``payload["indices"]``,
    ``{symbol: {current, ...}}``) but ``_baseline_indices`` reads
    ``market_snapshot["baseline"]["indices"]`` — so wrap. Returns ``{}`` when no
    market snapshot / no indices are present (missing != fabricated baseline).
    """
    for _item, snap in bundle_pairs:
        if getattr(snap, "snapshot_kind", None) != "market":
            continue
        indices = (getattr(snap, "payload_json", None) or {}).get("indices")
        if isinstance(indices, dict) and indices:
            return {"baseline": {"indices": indices}}
        return {}
    return {}


def _project_portfolio_snapshot(bundle_pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """ROB-470 — project a bundle ``portfolio`` snapshot into the lightweight
    ``{holdings: [{ticker, pnl_rate}]}`` shape the delta loader's holdings
    fallback reads.

    Keeps only ``ticker``/``pnl_rate`` per holding (lightweight; the heavy
    payload stays in the bundle). Returns ``{}`` when no portfolio snapshot or no
    usable holdings are present so an absent baseline is never fabricated.
    """
    for _item, snap in bundle_pairs:
        if getattr(snap, "snapshot_kind", None) != "portfolio":
            continue
        holdings = (getattr(snap, "payload_json", None) or {}).get("holdings")
        if not isinstance(holdings, list):
            return {}
        projected = [
            {"ticker": h.get("ticker"), "pnl_rate": h.get("pnl_rate")}
            for h in holdings
            if isinstance(h, dict) and h.get("ticker") is not None
        ]
        return {"holdings": projected} if projected else {}
    return {}


class HermesCompositionIngestError(RuntimeError):
    """Raised when the Hermes-produced envelope cannot be ingested."""


class HermesStageArtifactsIngestError(RuntimeError):
    """Raised when the stage-artifacts envelope cannot be ingested.

    Carries a ``code`` field so the MCP tool can serialise a structured
    error envelope without leaking server-side internals.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HermesStageArtifactIngestResult:
    """One row's outcome from the stage-artifact ingest."""

    stage_type: str
    artifact: InvestmentStageArtifact
    idempotent_existing: bool


@dataclass(frozen=True)
class HermesStageArtifactsIngestResponse:
    run: InvestmentStageRun
    results: list[HermesStageArtifactIngestResult]


# ---------------------------------------------------------------------------
# Stage-artifacts ingest
# ---------------------------------------------------------------------------


class HermesStageArtifactsIngestService:
    """Validate + persist Hermes-produced stage artifacts.

    Locked decisions referenced by this service:

    * §D1 — Hermes generates ``run_uuid`` client-side.
    * §D3 — Content-aware idempotency on ``(run_uuid, stage_type)``.
    * §D4 — Run stays in ``running``; composition ingest finalizes.
    * §D5 — No ordering enforced.
    * §D8 — Multiple artifacts per call.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        stages_repository: InvestmentStagesRepository | None = None,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._stages = stages_repository or InvestmentStagesRepository(session)
        self._snapshots = snapshots_repository or InvestmentSnapshotsRepository(session)

    async def ingest_stage_artifacts(
        self, request: HermesStageArtifactsIngestRequest
    ) -> HermesStageArtifactsIngestResponse:
        envelope = request.run_envelope

        bundle = await self._snapshots.get_bundle_by_uuid(envelope.snapshot_bundle_uuid)
        if bundle is None:
            raise HermesStageArtifactsIngestError(
                f"snapshot bundle not found: {envelope.snapshot_bundle_uuid}",
                code="snapshot_bundle_not_found",
            )

        run = await self._find_or_create_run(envelope)

        results: list[HermesStageArtifactIngestResult] = []
        for payload in request.artifacts:
            persisted, idempotent = await self._persist_or_reuse_artifact(
                run_uuid=run.run_uuid, payload=payload
            )
            results.append(
                HermesStageArtifactIngestResult(
                    stage_type=payload.stage_type,
                    artifact=persisted,
                    idempotent_existing=idempotent,
                )
            )

        return HermesStageArtifactsIngestResponse(run=run, results=results)

    # ------------------------------------------------------------------
    # Run helpers
    # ------------------------------------------------------------------

    async def _find_or_create_run(self, envelope: Any) -> InvestmentStageRun:
        existing = await self._stages.get_run(envelope.run_uuid)
        if existing is None:
            return await self._stages.create_run(
                run_uuid=envelope.run_uuid,
                snapshot_bundle_uuid=envelope.snapshot_bundle_uuid,
                market=envelope.market,
                market_session=envelope.market_session,
                account_scope=envelope.account_scope,
                policy_version=envelope.policy_version,
                generator_version=envelope.generator_version,
            )

        # Run exists — verify envelope consistency. A single run_uuid must
        # map to a single (bundle, market, session, scope, policy, generator).
        mismatches: list[str] = []
        if existing.snapshot_bundle_uuid != envelope.snapshot_bundle_uuid:
            mismatches.append(
                f"snapshot_bundle_uuid (existing={existing.snapshot_bundle_uuid}, "
                f"envelope={envelope.snapshot_bundle_uuid})"
            )
        if existing.market != envelope.market:
            mismatches.append(
                f"market (existing={existing.market!r}, envelope={envelope.market!r})"
            )
        if (existing.market_session or None) != (envelope.market_session or None):
            mismatches.append(
                f"market_session (existing={existing.market_session!r}, "
                f"envelope={envelope.market_session!r})"
            )
        if (existing.account_scope or None) != (envelope.account_scope or None):
            mismatches.append(
                f"account_scope (existing={existing.account_scope!r}, "
                f"envelope={envelope.account_scope!r})"
            )
        if existing.policy_version != envelope.policy_version:
            mismatches.append(
                f"policy_version (existing={existing.policy_version!r}, "
                f"envelope={envelope.policy_version!r})"
            )
        if existing.generator_version != envelope.generator_version:
            mismatches.append(
                f"generator_version (existing={existing.generator_version!r}, "
                f"envelope={envelope.generator_version!r})"
            )
        if mismatches:
            raise HermesStageArtifactsIngestError(
                "stage run envelope inconsistent with existing row for "
                f"run_uuid={envelope.run_uuid}: " + "; ".join(mismatches),
                code="run_envelope_mismatch",
            )
        return existing

    # ------------------------------------------------------------------
    # Artifact helpers
    # ------------------------------------------------------------------

    async def _persist_or_reuse_artifact(
        self, *, run_uuid: uuid.UUID, payload: StageArtifactPayload
    ) -> tuple[InvestmentStageArtifact, bool]:
        """Look-before-leap idempotent persist.

        ``persist_artifact`` raises ``AppendOnlyViolation`` on the
        underlying ``IntegrityError``, but that also leaves the
        SQLAlchemy async session in a rolled-back state — subsequent
        reads on the same session would hit ``PendingRollbackError``.
        Probe for an existing row first; if absent, insert. The
        existence check + insert run in the same transaction so a true
        concurrent insert by another worker would still surface as
        ``AppendOnlyViolation`` (acceptable: Hermes is single-flight
        per ``run_uuid`` in practice, and the bubbled error message
        identifies the race window for operator triage).
        """
        existing = await self._fetch_existing(run_uuid, payload.stage_type)
        if existing is not None:
            if not _artifact_content_matches(existing, payload):
                raise HermesStageArtifactsIngestError(
                    f"stage artifact ({run_uuid}, {payload.stage_type}) "
                    "already exists with different content; re-ingest with "
                    "mutated payload is rejected (append-only contract — "
                    "§D3).",
                    code="artifact_content_conflict",
                )
            return existing, True

        try:
            persisted = await self._stages.persist_artifact(run_uuid, payload)
        except AppendOnlyViolation as exc:
            raise HermesStageArtifactsIngestError(
                f"append-only race for ({run_uuid}, {payload.stage_type}): "
                "an artifact appeared between the existence probe and the "
                "insert. Retry the ingest.",
                code="append_only_race",
            ) from exc
        return persisted, False

    async def _fetch_existing(
        self, run_uuid: uuid.UUID, stage_type: str
    ) -> InvestmentStageArtifact | None:
        artifacts = await self._stages.list_artifacts_for_run(run_uuid)
        for art in artifacts:
            if art.stage_type == stage_type:
                return art
        return None


def _artifact_content_matches(
    existing: InvestmentStageArtifact, payload: StageArtifactPayload
) -> bool:
    """Compare every payload-derived field on the stored artifact against
    the incoming payload. Server-set fields (``id``, ``created_at``,
    ``run_uuid``, ``stage_type``) are intentionally excluded — they are
    either keys or non-payload."""
    if existing.verdict != payload.verdict.value:
        return False
    if int(existing.confidence) != int(payload.confidence):
        return False
    if (existing.summary or "") != (payload.summary or ""):
        return False
    if list(existing.key_points or []) != list(payload.key_points):
        return False
    if list(existing.buy_evidence or []) != list(payload.buy_evidence):
        return False
    if list(existing.sell_evidence or []) != list(payload.sell_evidence):
        return False
    if list(existing.risk_evidence or []) != list(payload.risk_evidence):
        return False
    if list(existing.missing_data or []) != list(payload.missing_data):
        return False
    incoming_citations = [str(c.snapshot_uuid) for c in payload.cited_snapshots]
    existing_citations = [str(u) for u in (existing.cited_snapshot_uuids or [])]
    if existing_citations != incoming_citations:
        return False
    if dict(existing.freshness_summary or {}) != dict(payload.freshness_summary or {}):
        return False
    if (existing.model_name or None) != (payload.model_name or None):
        return False
    if (existing.prompt_version or None) != (payload.prompt_version or None):
        return False
    return True


# ---------------------------------------------------------------------------
# Composition ingest
# ---------------------------------------------------------------------------


class HermesCompositionIngestService:
    """Validate + persist a Hermes-composed report.

    On successful ingest, this service also auto-finalizes any Hermes
    stage run referenced from ``composition.metadata.investment_stage_run_uuid``:
    a matching run row in the ``running`` state is transitioned to
    ``completed`` (§D4). Runs in any other state are left untouched and
    a single ``info`` log line is emitted so operators can investigate.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        ingestion_service: InvestmentReportIngestionService | None = None,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
        stages_repository: InvestmentStagesRepository | None = None,
        symbol_reports_repository: SymbolIntermediateReportRepository | None = None,
        dimension_reports_repository: DimensionReportRepository | None = None,
        news_service: InvestmentReportNewsService | None = None,
    ) -> None:
        self._session = session
        self._ingestion = ingestion_service or InvestmentReportIngestionService(session)
        self._snapshots = snapshots_repository or InvestmentSnapshotsRepository(session)
        self._stages = stages_repository or InvestmentStagesRepository(session)
        self._symbol_reports = (
            symbol_reports_repository or SymbolIntermediateReportRepository(session)
        )
        self._dimension_reports = (
            dimension_reports_repository or DimensionReportRepository(session)
        )
        self._news_service = news_service or InvestmentReportNewsService(
            InvestmentReportsRepository(session)
        )

    async def ingest_composition(
        self, request: HermesCompositionIngestRequest
    ) -> InvestmentReport:
        composition = request.composition
        bundle = await self._snapshots.get_bundle_by_uuid(
            composition.snapshot_bundle_uuid
        )
        if bundle is None:
            raise HermesCompositionIngestError(
                f"snapshot bundle not found: {composition.snapshot_bundle_uuid}"
            )

        metadata: dict[str, Any] = {
            **dict(composition.metadata),
            "hermes_composition": {
                "composition_version": composition.composition_version,
                "hermes_run_id": composition.hermes_run_id,
                "cited_snapshot_uuids": [
                    str(u) for u in composition.cited_snapshot_uuids
                ],
            },
            "snapshot_backed_generator": True,
            "generator_signature": {
                "report_type": request.report_type,
                "policy_version": request.policy_version,
                "generator_version": request.generator_version,
            },
        }

        # ROB-301 D3: validate + attach the symbol-report references. Empty for
        # legacy composition, in which case metadata is unchanged (REGRESSION).
        symbol_report_refs = await self._validate_symbol_report_refs(
            composition.symbol_intermediate_report_uuids,
            stage_run_uuid=composition.metadata.get("investment_stage_run_uuid"),
        )
        if symbol_report_refs:
            metadata["symbol_intermediate_report_uuids"] = symbol_report_refs

        # ROB-308: validate + attach the dimension-report references. Empty for
        # legacy composition.
        dimension_report_refs = await self._validate_dimension_report_refs(
            composition.dimension_report_uuids,
            stage_run_uuid=composition.metadata.get("investment_stage_run_uuid"),
        )
        if dimension_report_refs:
            metadata["dimension_report_uuids"] = dimension_report_refs

        # ROB-470 — project the bundle's portfolio/market snapshots into the
        # report's lightweight JSON columns so delta_get's holdings/index deltas
        # work for Hermes-created reports (index in particular is read ONLY from
        # report.market_snapshot, never the bundle). Fetched once and reused for
        # the news projection below.
        bundle_pairs = await self._snapshots.list_bundle_items_with_snapshots(bundle.id)
        market_snapshot = _project_market_snapshot(bundle_pairs)
        portfolio_snapshot = _project_portfolio_snapshot(bundle_pairs)

        ingest_request = IngestReportRequest(
            report_type=request.report_type,
            market=cast(MarketLiteral, request.market),
            market_session=cast(MarketSessionLiteral | None, request.market_session),
            account_scope=cast(AccountScopeLiteral | None, request.account_scope),
            execution_mode="advisory_only",
            created_by_profile=request.created_by_profile,
            title=composition.title,
            summary=composition.summary,
            risk_summary=composition.risk_summary,
            thesis_text=composition.thesis_text,
            no_action_note=composition.no_action_note,
            status=request.status,
            kst_date=request.kst_date,
            items=list(composition.items),
            market_snapshot=market_snapshot,
            portfolio_snapshot=portfolio_snapshot,
            snapshot_bundle_uuid=composition.snapshot_bundle_uuid,
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary=dict(bundle.coverage_summary or {}),
            snapshot_freshness_summary=dict(bundle.freshness_summary or {}),
            source_conflicts={},
            unavailable_sources={},
            metadata=metadata,
            generator_version=request.generator_version or HERMES_COMPOSITION_VERSION,
        )

        report = await self._ingestion.ingest(ingest_request)

        # ROB-423 — persist Hermes-marked news citations from the bundle's news
        # snapshot. Fail-open: matching gaps never block report creation.
        news_payloads = [
            (snap.payload_json or {})
            for _item, snap in bundle_pairs
            if snap.snapshot_kind == "news"
        ]
        await self._news_service.persist_from_composition(
            report=report, composition=composition, news_payloads=news_payloads
        )

        await self._maybe_finalize_stage_run(composition.metadata)
        return report

    async def _validate_symbol_report_refs(
        self,
        symbol_report_uuids: list[uuid.UUID],
        *,
        stage_run_uuid: Any = None,
    ) -> list[str]:
        """Validate referenced symbol reports exist (Codex #12). When the
        composition names its stage run, every referenced report must belong to
        it (cross-run UUID mixups rejected). Returns string UUIDs for the
        metadata reference; empty input returns ``[]`` (legacy path)."""
        if not symbol_report_uuids:
            return []
        found = await self._symbol_reports.get_by_uuids(list(symbol_report_uuids))
        found_by_uuid = {r.symbol_report_uuid: r for r in found}
        missing = [str(u) for u in symbol_report_uuids if u not in found_by_uuid]
        if missing:
            raise HermesCompositionIngestError(
                "composition references unknown symbol intermediate reports: "
                + ", ".join(missing)
            )
        parsed_run = _coerce_uuid(stage_run_uuid)
        if parsed_run is not None:
            wrong_run = [
                str(u) for u, r in found_by_uuid.items() if r.run_uuid != parsed_run
            ]
            if wrong_run:
                raise HermesCompositionIngestError(
                    "symbol intermediate reports do not belong to stage run "
                    f"{parsed_run}: {', '.join(wrong_run)}"
                )
        return [str(u) for u in symbol_report_uuids]

    async def _validate_dimension_report_refs(
        self,
        dimension_report_uuids: list[uuid.UUID],
        *,
        stage_run_uuid: Any = None,
    ) -> list[str]:
        """Validate referenced dimension reports exist. When the
        composition names its stage run, every referenced report must belong to
        it (cross-run UUID mixups rejected). Returns string UUIDs for the
        metadata reference; empty input returns ``[]`` (legacy path)."""
        if not dimension_report_uuids:
            return []
        found = await self._dimension_reports.get_by_uuids(list(dimension_report_uuids))
        found_by_uuid = {r.dimension_report_uuid: r for r in found}
        missing = [str(u) for u in dimension_report_uuids if u not in found_by_uuid]
        if missing:
            raise HermesCompositionIngestError(
                f"dimension reports not found: {missing}"
            )
        parsed_run = _coerce_uuid(stage_run_uuid)
        if parsed_run is not None:
            wrong_run = [
                str(u) for u, r in found_by_uuid.items() if r.run_uuid != parsed_run
            ]
            if wrong_run:
                raise HermesCompositionIngestError(
                    f"dimension reports not in run {parsed_run}: {wrong_run}"
                )
        return [str(u) for u in dimension_report_uuids]

    async def _maybe_finalize_stage_run(
        self, composition_metadata: dict[str, Any]
    ) -> None:
        """If composition metadata references a Hermes stage run that's
        still ``running``, transition it to ``completed`` (§D4).
        Best-effort: malformed UUID, missing run, or non-``running``
        state all noop with an ``info`` log line so the composition
        ingest itself stays the authoritative outcome."""
        raw_uuid = composition_metadata.get("investment_stage_run_uuid")
        if raw_uuid is None:
            return
        if not isinstance(raw_uuid, str | uuid.UUID):
            _logger.info(
                "hermes_composition: skipping stage-run finalize — "
                "metadata.investment_stage_run_uuid is %r (expected str/UUID)",
                type(raw_uuid).__name__,
            )
            return
        try:
            parsed = uuid.UUID(str(raw_uuid))
        except (TypeError, ValueError):
            _logger.info(
                "hermes_composition: skipping stage-run finalize — invalid UUID %r",
                raw_uuid,
            )
            return

        run = await self._stages.get_run(parsed)
        if run is None:
            _logger.info(
                "hermes_composition: stage-run finalize skipped — run %s not found",
                parsed,
            )
            return
        if run.status != "running":
            _logger.info(
                "hermes_composition: stage-run %s already in terminal state %r; "
                "not re-transitioning",
                parsed,
                run.status,
            )
            return
        await self._stages.complete_run(parsed, status="completed")


__all__ = [
    "HermesCompositionIngestError",
    "HermesCompositionIngestService",
    "HermesStageArtifactIngestResult",
    "HermesStageArtifactsIngestError",
    "HermesStageArtifactsIngestResponse",
    "HermesStageArtifactsIngestService",
]


# Backwards-compat shim — earlier code imported a no-op ``_uuid_str``
# helper from this module. Drop only after grep across consumers shows
# zero remaining references (none today).
def _uuid_str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    """Parse a str/UUID into a UUID, returning None for anything unparseable."""
    if value is None or not isinstance(value, str | uuid.UUID):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None

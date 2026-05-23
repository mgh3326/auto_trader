"""Hermes symbol-reduction ingest service (ROB-301).

Validates + persists Hermes-pushed per-symbol judgments (D9 push-only — never
calls an LLM in-process). Mirrors ``HermesStageArtifactsIngestService``.

Locked decisions:

* D2 — symbol reports belong to the EXISTING ``investment_stage_runs`` run; the
  run must already exist (symbol reduction consumes its stage artifacts).
* D11 — ``verdict`` is service-derived from ``(decision_bucket, side,
  availability)``; Hermes cannot supply it (schema omits the field).
  ``content_hash`` drives idempotency + version bump.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageRun
from app.models.investment_symbol_intermediate_reports import (
    InvestmentSymbolIntermediateReport,
)
from app.schemas.investment_symbol_reports import (
    HermesSymbolReductionResult,
    HermesSymbolReportsIngestRequest,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
from app.services.investment_stages.repository import InvestmentStagesRepository
from app.services.investment_stages.symbol_report_repository import (
    SymbolIntermediateReportRepository,
    SymbolReportPersistRace,
)


class SymbolReportIngestError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SymbolReportIngestResult:
    symbol: str
    report: InvestmentSymbolIntermediateReport
    idempotent_existing: bool


@dataclass(frozen=True)
class SymbolReportsIngestResponse:
    run: InvestmentStageRun
    results: list[SymbolReportIngestResult]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def derive_verdict(
    payload: HermesSymbolReductionResult,
) -> tuple[str, str, str | None]:
    """Return ``(verdict, decision_bucket, unavailable_reason)`` derived from the
    payload. Hermes never supplies verdict (D11); the service is authoritative.

    The mapping satisfies the model's ``verdict``/``decision_bucket`` CHECKs and
    the unavailable->deferred_no_action pin.
    """
    if not payload.data_available:
        return "unavailable", "deferred_no_action", "data_unavailable"

    bucket = payload.decision_bucket
    side = payload.side
    if bucket == "new_buy_candidate":
        verdict = "buy"
    elif bucket in ("completed_or_existing", "deferred_no_action"):
        verdict = "hold"
    elif bucket == "open_action":
        if side not in ("buy", "sell"):
            raise SymbolReportIngestError(
                f"{payload.symbol}: open_action requires side in (buy, sell)",
                code="open_action_missing_side",
            )
        verdict = side
    elif bucket == "risk_watch":
        verdict = "sell" if side == "sell" else "risk"
    else:  # defensive — the schema already constrains the vocabulary
        raise SymbolReportIngestError(
            f"{payload.symbol}: unknown decision_bucket {bucket!r}",
            code="unknown_bucket",
        )
    return verdict, bucket, None


def content_hash(
    payload: HermesSymbolReductionResult,
    *,
    verdict: str,
    decision_bucket: str,
    unavailable_reason: str | None,
) -> str:
    """Canonical sha256 over the derived + payload fields. Sorted keys + sorted
    UUID lists make it order-insensitive, so two retries with identical meaning
    hash equal (drives idempotent upsert)."""
    canonical: dict[str, Any] = {
        "symbol": payload.symbol,
        "symbol_name": payload.symbol_name,
        "data_available": payload.data_available,
        "decision_bucket": decision_bucket,
        "verdict": verdict,
        "unavailable_reason": unavailable_reason,
        "side": payload.side,
        "confidence": payload.confidence,
        "summary": payload.summary,
        "rationale": payload.rationale,
        "buy_evidence": payload.buy_evidence or [],
        "sell_evidence": payload.sell_evidence or [],
        "risk_evidence": payload.risk_evidence or [],
        "missing_data": payload.missing_data or [],
        "freshness_summary": payload.freshness_summary or {},
        "cited_snapshot_uuids": sorted(str(u) for u in payload.cited_snapshot_uuids),
        "source_stage_artifact_uuids": sorted(
            str(u) for u in payload.source_stage_artifact_uuids
        ),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _assert_run_matches(run: InvestmentStageRun, envelope: Any) -> None:
    mismatches: list[str] = []
    if run.snapshot_bundle_uuid != envelope.snapshot_bundle_uuid:
        mismatches.append("snapshot_bundle_uuid")
    if run.market != envelope.market:
        mismatches.append("market")
    if (run.market_session or None) != (envelope.market_session or None):
        mismatches.append("market_session")
    if (run.account_scope or None) != (envelope.account_scope or None):
        mismatches.append("account_scope")
    if mismatches:
        raise SymbolReportIngestError(
            f"symbol-reports envelope inconsistent with stage run "
            f"{envelope.run_uuid}: {', '.join(mismatches)}",
            code="run_envelope_mismatch",
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SymbolIntermediateReportIngestService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        stages_repository: InvestmentStagesRepository | None = None,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
        reports_repository: SymbolIntermediateReportRepository | None = None,
    ) -> None:
        self._session = session
        self._stages = stages_repository or InvestmentStagesRepository(session)
        self._snapshots = snapshots_repository or InvestmentSnapshotsRepository(session)
        self._reports = reports_repository or SymbolIntermediateReportRepository(
            session
        )

    async def ingest_from_hermes(
        self, request: HermesSymbolReportsIngestRequest
    ) -> SymbolReportsIngestResponse:
        envelope = request.run_envelope

        bundle = await self._snapshots.get_bundle_by_uuid(envelope.snapshot_bundle_uuid)
        if bundle is None:
            raise SymbolReportIngestError(
                f"snapshot bundle not found: {envelope.snapshot_bundle_uuid}",
                code="snapshot_bundle_not_found",
            )

        run = await self._resolve_run(envelope)

        results: list[SymbolReportIngestResult] = []
        for payload in request.symbol_reports:
            report, idempotent = await self._persist_or_reuse(
                run=run, report_kind=request.report_kind, payload=payload
            )
            results.append(
                SymbolReportIngestResult(
                    symbol=payload.symbol,
                    report=report,
                    idempotent_existing=idempotent,
                )
            )
        return SymbolReportsIngestResponse(run=run, results=results)

    async def _resolve_run(self, envelope: Any) -> InvestmentStageRun:
        run = await self._stages.get_run(envelope.run_uuid)
        if run is None:
            raise SymbolReportIngestError(
                f"stage run not found: {envelope.run_uuid}. Ingest stage "
                "artifacts before symbol reports (symbol reduction consumes "
                "the run's cross-symbol artifacts).",
                code="stage_run_not_found",
            )
        _assert_run_matches(run, envelope)
        return run

    async def _persist_or_reuse(
        self,
        *,
        run: InvestmentStageRun,
        report_kind: str,
        payload: HermesSymbolReductionResult,
    ) -> tuple[InvestmentSymbolIntermediateReport, bool]:
        verdict, decision_bucket, unavailable_reason = derive_verdict(payload)
        digest = content_hash(
            payload,
            verdict=verdict,
            decision_bucket=decision_bucket,
            unavailable_reason=unavailable_reason,
        )
        key = f"{run.run_uuid}:{payload.symbol}:{report_kind}:{digest}"

        # Look-before-leap: identical content (same key) returns the stored row
        # without touching the session's transaction state (D11 idempotent).
        existing = await self._reports.get_by_idempotency_key(key)
        if existing is not None:
            return existing, True

        version = await self._reports.next_version(
            run_uuid=run.run_uuid, symbol=payload.symbol, report_kind=report_kind
        )
        try:
            report = await self._reports.persist(
                run_uuid=run.run_uuid,
                snapshot_bundle_uuid=run.snapshot_bundle_uuid,
                market=run.market,
                account_scope=run.account_scope,
                symbol=payload.symbol,
                symbol_name=payload.symbol_name,
                report_kind=report_kind,
                artifact_version=version,
                decision_bucket=decision_bucket,
                verdict=verdict,
                unavailable_reason=unavailable_reason,
                confidence=payload.confidence,
                summary=payload.summary,
                rationale=payload.rationale,
                buy_evidence=payload.buy_evidence,
                sell_evidence=payload.sell_evidence,
                risk_evidence=payload.risk_evidence,
                missing_data=payload.missing_data,
                freshness_summary=payload.freshness_summary,
                content_hash=digest,
                source_stage_artifact_uuids=list(payload.source_stage_artifact_uuids),
                cited_snapshot_uuids=list(payload.cited_snapshot_uuids),
                idempotency_key=key,
            )
        except SymbolReportPersistRace as exc:
            raise SymbolReportIngestError(
                f"append-only/version race for ({run.run_uuid}, {payload.symbol}): "
                "a row appeared between the existence probe and the insert. "
                "Retry the ingest.",
                code="symbol_report_race",
            ) from exc
        return report, False

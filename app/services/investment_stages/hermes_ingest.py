"""Hermes composition ingest service (ROB-287).

Takes a Hermes-produced :class:`HermesCompositionResult`, builds an
:class:`IngestReportRequest`, and persists through
:class:`InvestmentReportIngestionService`. The existing
``enforce_stale_gate_for_ingest`` + idempotency-key pipeline is the
authoritative gate; this service only validates the Hermes-shaped
envelope and wires bundle metadata back onto the request so provenance
survives.

Hard invariants:

* No external LLM is called here. Hermes does its reasoning out of
  process and hands a frozen JSON shape back through this contract.
* All items must be ``operation`` ∈ ``{review, cancel, keep}`` with
  ``apply_policy='requires_user_approval'`` — the schema validator
  rejects anything else outright.
* ``snapshot_bundle_uuid`` is mandatory and round-trips into the
  ingested row's metadata, so the report stays linked to the source
  bundle for audit.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport
from app.schemas.hermes_composition import (
    HERMES_COMPOSITION_VERSION,
    HermesCompositionIngestRequest,
)
from app.schemas.investment_reports import (
    AccountScopeLiteral,
    IngestReportRequest,
    MarketLiteral,
    MarketSessionLiteral,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

_logger = logging.getLogger(__name__)


class HermesCompositionIngestError(RuntimeError):
    """Raised when the Hermes-produced envelope cannot be ingested."""


class HermesCompositionIngestService:
    """Validate + persist a Hermes-composed report."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        ingestion_service: InvestmentReportIngestionService | None = None,
        snapshots_repository: InvestmentSnapshotsRepository | None = None,
    ) -> None:
        self._session = session
        self._ingestion = ingestion_service or InvestmentReportIngestionService(session)
        self._snapshots = snapshots_repository or InvestmentSnapshotsRepository(session)

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

        # ``items`` already passed the advisory-only validator in
        # :class:`HermesCompositionResult`. We carry them through unchanged so
        # the ingestion service's own idempotency and stale gate stay
        # authoritative.
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
            snapshot_bundle_uuid=composition.snapshot_bundle_uuid,
            snapshot_policy_version=request.policy_version,
            snapshot_coverage_summary=dict(bundle.coverage_summary or {}),
            snapshot_freshness_summary=dict(bundle.freshness_summary or {}),
            source_conflicts={},
            unavailable_sources={},
            metadata=metadata,
            generator_version=request.generator_version or HERMES_COMPOSITION_VERSION,
        )

        return await self._ingestion.ingest(ingest_request)


__all__ = [
    "HermesCompositionIngestError",
    "HermesCompositionIngestService",
]


def _uuid_str(value: uuid.UUID | None) -> str | None:
    """Internal helper, kept for symmetry with potential future usage."""
    return str(value) if value is not None else None

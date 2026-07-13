"""Cohort-owned cancel boundary over ROB-845 paper execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.composition import build_paper_execution_application
from app.services.brokers.paper.contracts import (
    ExperimentProvenanceVerifier,
    PaperOperation,
    PaperOperationResult,
    PaperOrderRequest,
    PaperReasonCode,
)
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_cohort.runner import PaperCohortRunner
from app.services.paper_cohort.signals import CanonicalTargetSignal


class CancelApplication(Protocol):
    async def cancel(self, request) -> PaperOperationResult: ...  # noqa: ANN001


class PaperCohortOrderControl:
    def __init__(
        self,
        session: AsyncSession,
        *,
        verifier: ExperimentProvenanceVerifier,
        application_factory: (
            Callable[[ExperimentProvenanceVerifier], CancelApplication] | None
        ) = None,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._application_factory = application_factory

    async def _owned_request(self, cohort_id: str, link_id: int) -> PaperOrderRequest:
        link = await self._session.get(PaperRunOrderLink, link_id)
        if link is None or link.cohort_id != cohort_id:
            raise PaperCohortError("cohort_order_not_owned")
        intent = await self._session.scalar(
            select(PaperCohortVenueIntent).where(
                PaperCohortVenueIntent.cohort_id == link.cohort_id,
                PaperCohortVenueIntent.run_id == link.run_id,
                PaperCohortVenueIntent.decision_id == link.decision_id,
                PaperCohortVenueIntent.venue == link.venue,
            )
        )
        decision = await self._session.scalar(
            select(PaperCohortDecision).where(
                PaperCohortDecision.decision_id
                == ("" if intent is None else intent.decision_id)
            )
        )
        snapshot = await self._session.scalar(
            select(CanonicalMarketSnapshot).where(
                CanonicalMarketSnapshot.snapshot_id == link.snapshot_id
            )
        )
        if intent is None or decision is None or snapshot is None:
            raise PaperCohortError("cohort_order_identity_mismatch")
        if not all(
            (
                intent.snapshot_id == link.snapshot_id,
                intent.snapshot_hash == link.snapshot_hash,
                decision.snapshot_id == link.snapshot_id,
                decision.snapshot_hash == link.snapshot_hash,
            )
        ):
            raise PaperCohortError("cohort_order_identity_mismatch")
        return PaperCohortRunner.build_request(
            intent,
            CanonicalTargetSignal.model_validate(decision.signal_payload),
            intent.would_order_evidence,
            CanonicalSnapshotPayload.model_validate(snapshot.payload),
        )

    async def cancel(self, cohort_id: str, link_id: int) -> PaperOperationResult:
        request = await self._owned_request(cohort_id, link_id)
        application = (
            build_paper_execution_application(verifier=self._verifier)
            if self._application_factory is None
            else self._application_factory(self._verifier)
        )
        return await application.cancel(request)

    async def close(self, cohort_id: str, link_id: int) -> PaperOperationResult:
        request = await self._owned_request(cohort_id, link_id)
        return PaperOperationResult.blocked(
            operation=PaperOperation.CANCEL,
            venue=Broker(request.venue),
            reason_code=PaperReasonCode.UNSUPPORTED_CAPABILITY,
            evidence={"operation": "close"},
        )


__all__ = ["PaperCohortOrderControl"]

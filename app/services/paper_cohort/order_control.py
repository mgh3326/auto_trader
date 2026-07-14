"""Cohort-owned cancel/close boundary over ROB-845 paper execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    VerifiedExperimentProvenance,
)
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_cohort.native_links import NativeOrderResolver
from app.services.paper_cohort.runner import PaperCohortRunner
from app.services.paper_cohort.signals import CanonicalTargetSignal


class OrderControlApplication(Protocol):
    async def cancel(self, request: PaperOrderRequest) -> PaperOperationResult: ...

    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult: ...


@dataclass(frozen=True)
class _OwnedOperationVerifier:
    original: PaperOrderRequest
    evidence: VerifiedExperimentProvenance
    source_buy_client_order_id: str | None = None

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
        if request.side == "buy" and request != self.original:
            raise PaperCohortError("cohort_order_identity_mismatch")
        return VerifiedExperimentProvenance(
            **request.model_dump(),
            decision_id=self.evidence.decision_id,
            reference_price=self.evidence.reference_price,
            source_buy_client_order_id=self.source_buy_client_order_id,
        )


class PaperCohortOrderControl:
    def __init__(
        self,
        session: AsyncSession,
        *,
        verifier: ExperimentProvenanceVerifier,
        application_factory: (
            Callable[[ExperimentProvenanceVerifier], OrderControlApplication] | None
        ) = None,
        native_resolver: NativeOrderResolver | None = None,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._application_factory = application_factory
        self._native_resolver = native_resolver or NativeOrderResolver(session)

    async def _owned_request(
        self, cohort_id: str, link_id: int
    ) -> tuple[PaperOrderRequest, PaperRunOrderLink, VerifiedExperimentProvenance]:
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
        native = await self._native_resolver.resolve(
            link.venue, link.client_order_id, link.broker_order_id
        )
        if not all(
            (
                native.ledger_kind == link.native_ledger_kind,
                native.ledger_row_id == link.native_ledger_row_id,
                native.client_order_id == link.client_order_id,
                native.broker_order_id == link.broker_order_id,
            )
        ):
            raise PaperCohortError("native_order_identity_mismatch")
        request = PaperCohortRunner.build_request(
            intent,
            CanonicalTargetSignal.model_validate(decision.signal_payload),
            intent.would_order_evidence,
            CanonicalSnapshotPayload.model_validate(snapshot.payload),
        )
        verify_persisted = getattr(self._verifier, "verify_persisted", None)
        if verify_persisted is None:
            raise PaperCohortError("recovery_verifier_unavailable")
        evidence = await verify_persisted(request)
        return request, link, evidence

    def _application(
        self, verifier: ExperimentProvenanceVerifier
    ) -> OrderControlApplication:
        if self._application_factory is None:
            return build_paper_execution_application(verifier=verifier)
        return self._application_factory(verifier)

    async def cancel(self, cohort_id: str, link_id: int) -> PaperOperationResult:
        request, _link, evidence = await self._owned_request(cohort_id, link_id)
        return await self._application(
            _OwnedOperationVerifier(request, evidence)
        ).cancel(request)

    async def close(self, cohort_id: str, link_id: int) -> PaperOperationResult:
        request, link, evidence = await self._owned_request(cohort_id, link_id)
        if request.venue is not Broker.ALPACA or request.qty is None:
            return PaperOperationResult.blocked(
                operation=PaperOperation.SUBMIT,
                venue=Broker(request.venue),
                reason_code=PaperReasonCode.UNSUPPORTED_CAPABILITY,
                evidence={"operation": "close"},
            )
        close_request = request.model_copy(
            update={
                "intent_id": f"{request.intent_id}:close:{link.id}",
                "side": "sell",
                "source_buy_reference": link.client_order_id,
            }
        )
        return await self._application(
            _OwnedOperationVerifier(
                request,
                evidence,
                source_buy_client_order_id=link.client_order_id,
            )
        ).submit(close_request)


__all__ = ["PaperCohortOrderControl"]

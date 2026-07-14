"""Cohort-owned cancel/close boundary over ROB-845 paper execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
)
from app.services.alpaca_paper_ledger_service import (
    KNOWN_OPEN_BROKER_STATUSES,
    KNOWN_TERMINAL_BROKER_STATUSES,
    normalize_known_broker_order_status,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.composition import build_paper_execution_application
from app.services.brokers.paper.contracts import (
    ExperimentProvenanceVerifier,
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
    PaperReasonCode,
    VerifiedExperimentProvenance,
)
from app.services.paper_cohort.contracts import (
    PaperCohortError,
    PaperCohortLinkCleanupResult,
)
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_cohort.native_links import NativeOrderResolver
from app.services.paper_cohort.runner import PaperCohortRunner
from app.services.paper_cohort.signals import CanonicalTargetSignal


class OrderControlApplication(Protocol):
    async def get_order(self, request: PaperOrderRequest) -> PaperOperationResult: ...

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
        return await self._close_owned(request, link, evidence)

    async def _close_owned(
        self,
        request: PaperOrderRequest,
        link: PaperRunOrderLink,
        evidence: VerifiedExperimentProvenance,
        *,
        filled_qty: Decimal | None = None,
    ) -> PaperOperationResult:
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
                "qty": filled_qty or request.qty,
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

    async def cleanup(
        self, cohort_id: str, link_id: int
    ) -> PaperCohortLinkCleanupResult:
        """Run one fail-closed cleanup pass for a cohort-owned native order.

        Cleanup reads persisted native truth before every side effect. Open orders
        are canceled before any close, and a close is attempted only when the
        persisted ledger proves a positive fill (or the native order is filled).
        """

        request, link, evidence = await self._owned_request(cohort_id, link_id)
        venue = Broker(request.venue)
        if venue is not Broker.ALPACA or request.qty is None:
            return self._cleanup_result(
                link,
                status="manual_required",
                action="none",
                reason_code="unsupported_capability",
            )

        application = self._application(_OwnedOperationVerifier(request, evidence))
        native = await application.get_order(request)
        if native.status is not PaperOperationStatus.SUCCEEDED:
            return self._cleanup_result(
                link,
                status="pending",
                action="none",
                reason_code="native_status_unavailable",
            )

        native_status = normalize_known_broker_order_status(
            native.evidence.get("order_status")
        )
        if native_status is None:
            return self._cleanup_result(
                link,
                status="pending",
                action="none",
                reason_code="native_status_unknown",
            )

        if native_status in KNOWN_OPEN_BROKER_STATUSES:
            cancel = await application.cancel(request)
            if cancel.status is not PaperOperationStatus.SUCCEEDED:
                return self._cleanup_result(
                    link,
                    status="pending",
                    action="cancel",
                    reason_code=(
                        "cancel_pending"
                        if cancel.status is PaperOperationStatus.BLOCKED
                        else "cancel_failed"
                    ),
                    replayed=cancel.replayed,
                )

            # Re-read persisted truth after the cancel acknowledgement. This
            # prevents a fill racing with cancellation from being under-closed.
            native = await application.get_order(request)
            if native.status is not PaperOperationStatus.SUCCEEDED:
                return self._cleanup_result(
                    link,
                    status="pending",
                    action="cancel",
                    reason_code="post_cancel_status_unavailable",
                    replayed=cancel.replayed,
                )
            native_status = normalize_known_broker_order_status(
                native.evidence.get("order_status")
            )
            if native_status is None:
                return self._cleanup_result(
                    link,
                    status="pending",
                    action="cancel",
                    reason_code="post_cancel_status_unknown",
                    replayed=cancel.replayed,
                )
            if native_status in KNOWN_OPEN_BROKER_STATUSES:
                return self._cleanup_result(
                    link,
                    status="pending",
                    action="cancel",
                    reason_code="cancel_pending",
                    replayed=cancel.replayed,
                )

        filled_qty = self._filled_qty(native.evidence.get("filled_qty"))
        if native_status == "filled" and filled_qty is None:
            # `filled` is a normalized, known Alpaca terminal status for the
            # immutable persisted buy request. It proves the full requested
            # quantity filled; filled_qty is redundant provider metadata here.
            filled_qty = request.qty
        elif filled_qty is None:
            return self._cleanup_result(
                link,
                status="pending",
                action="none",
                reason_code="native_filled_quantity_unknown",
            )

        if filled_qty == 0:
            if native_status not in KNOWN_TERMINAL_BROKER_STATUSES:
                return self._cleanup_result(
                    link,
                    status="pending",
                    action="none",
                    reason_code="native_order_not_terminal",
                )
            return self._cleanup_result(
                link,
                status="complete",
                action="none",
                reason_code="native_order_terminal",
            )

        closed = await self._close_owned(
            request,
            link,
            evidence,
            filled_qty=filled_qty,
        )
        if closed.status is PaperOperationStatus.SUCCEEDED:
            return self._cleanup_result(
                link,
                status="complete",
                action="close",
                reason_code="close_complete",
                replayed=closed.replayed,
            )
        if str(closed.reason_code) == PaperReasonCode.UNSUPPORTED_CAPABILITY.value:
            return self._cleanup_result(
                link,
                status="manual_required",
                action="close",
                reason_code="unsupported_capability",
            )
        return self._cleanup_result(
            link,
            status="pending",
            action="close",
            reason_code=(
                "close_pending"
                if closed.status is PaperOperationStatus.BLOCKED
                else "close_failed"
            ),
            replayed=closed.replayed,
        )

    @staticmethod
    def _filled_qty(value: object) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            quantity = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
        if not quantity.is_finite() or quantity < 0:
            return None
        return quantity

    @staticmethod
    def _cleanup_result(
        link: PaperRunOrderLink,
        *,
        status: str,
        action: str,
        reason_code: str,
        replayed: bool = False,
    ) -> PaperCohortLinkCleanupResult:
        return PaperCohortLinkCleanupResult.model_validate(
            {
                "link_id": link.id,
                "venue": link.venue,
                "status": status,
                "action": action,
                "reason_code": reason_code,
                "replayed": replayed,
            }
        )


__all__ = ["PaperCohortOrderControl"]

"""Provenance-first canonical application boundary for paper execution."""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from app.services.brokers.capabilities import (
    Broker,
    PaperBrokerCapabilities,
    get_paper_capabilities,
)
from app.services.brokers.paper.adapter_registry import PaperAdapterNotFound
from app.services.brokers.paper.contracts import (
    ExperimentProvenanceVerifier,
    PaperOperation,
    PaperOperationResult,
    PaperOrderRequest,
    PaperReasonCode,
    VerifiedExperimentProvenance,
    VerifiedPaperOrderIntent,
    derive_paper_idempotency_key,
)


class _AdapterResolver(Protocol):
    def resolve(self, broker: Broker): ...  # noqa: ANN202


class PaperExecutionApplication:
    def __init__(
        self,
        *,
        registry: _AdapterResolver,
        verifier: ExperimentProvenanceVerifier | None,
    ) -> None:
        self._registry = registry
        self._verifier = verifier

    async def preview(self, request: PaperOrderRequest) -> PaperOperationResult:
        return await self._dispatch(PaperOperation.PREVIEW, request)

    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult:
        return await self._dispatch(PaperOperation.SUBMIT, request)

    async def cancel(self, request: PaperOrderRequest) -> PaperOperationResult:
        return await self._dispatch(PaperOperation.CANCEL, request)

    async def get_order(self, request: PaperOrderRequest) -> PaperOperationResult:
        return await self._dispatch(PaperOperation.GET_ORDER, request)

    async def reconcile(self, request: PaperOrderRequest) -> PaperOperationResult:
        return await self._dispatch(PaperOperation.RECONCILE, request)

    async def _dispatch(
        self,
        operation: PaperOperation,
        request: PaperOrderRequest,
    ) -> PaperOperationResult:
        provenance_result = await self._verify(operation, request)
        if isinstance(provenance_result, PaperOperationResult):
            return provenance_result
        provenance = provenance_result

        mismatch = _first_provenance_mismatch(request, provenance)
        if mismatch is not None:
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.PROVENANCE_MISMATCH,
                evidence={"field": mismatch},
            )

        intent = VerifiedPaperOrderIntent(
            **request.model_dump(),
            decision_id=provenance.decision_id,
            reference_price=provenance.reference_price,
            source_buy_client_order_id=provenance.source_buy_client_order_id,
            origin="experiment",
            idempotency_key=derive_paper_idempotency_key(provenance),
        )
        capabilities = get_paper_capabilities(request.venue)
        if (
            capabilities is None
            or not capabilities.supports(operation)
            or not _intent_is_supported(request, capabilities)
        ):
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.UNSUPPORTED_CAPABILITY,
            )

        try:
            adapter = self._registry.resolve(request.venue)
        except PaperAdapterNotFound:
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.ADAPTER_UNAVAILABLE,
            )
        handler = getattr(adapter, operation.value)
        return await handler(intent)

    async def _verify(
        self,
        operation: PaperOperation,
        request: PaperOrderRequest,
    ) -> VerifiedExperimentProvenance | PaperOperationResult:
        if self._verifier is None:
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.PROVENANCE_VERIFIER_UNAVAILABLE,
            )
        try:
            raw = await self._verifier.verify(request)
        except Exception:
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.PROVENANCE_VERIFICATION_FAILED,
            )
        if not isinstance(raw, VerifiedExperimentProvenance):
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.PROVENANCE_EVIDENCE_INVALID,
            )
        try:
            return VerifiedExperimentProvenance.model_validate(raw.model_dump())
        except (AttributeError, TypeError, ValidationError, ValueError):
            return PaperOperationResult.blocked(
                operation=operation,
                venue=request.venue,
                reason_code=PaperReasonCode.PROVENANCE_EVIDENCE_INVALID,
            )


def _first_provenance_mismatch(
    request: PaperOrderRequest,
    provenance: VerifiedExperimentProvenance,
) -> str | None:
    for field_name in PaperOrderRequest.model_fields:
        if getattr(request, field_name) != getattr(provenance, field_name):
            return field_name
    return None


def _intent_is_supported(
    request: PaperOrderRequest,
    capabilities: PaperBrokerCapabilities,
) -> bool:
    sizing_mode = "qty" if request.qty is not None else "notional"
    time_in_force_supported = (
        request.time_in_force in capabilities.time_in_force
        if capabilities.time_in_force
        else request.time_in_force is None
    )
    price_supported = (
        request.price is not None
        if request.order_type == "limit"
        else request.price is None
    )
    return (
        request.account_mode == capabilities.account_mode
        and request.product in capabilities.products
        and request.symbol in capabilities.symbols
        and request.side in capabilities.sides
        and request.order_type in capabilities.order_types
        and time_in_force_supported
        and sizing_mode in capabilities.sizing_modes
        and request.market_snapshot_source == capabilities.quote_source
        and price_supported
    )


__all__ = ["ExperimentProvenanceVerifier", "PaperExecutionApplication"]

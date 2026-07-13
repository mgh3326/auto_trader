"""Canonical Alpaca Crypto Paper adapter over the guarded application service."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.alpaca_paper_order_application import (
    AlpacaPaperApplicationOutcome,
    AlpacaPaperOrderApplication,
    AlpacaPaperOrderSpec,
    AlpacaVerifiedDecision,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperReasonCode,
    VerifiedPaperOrderIntent,
)


class _AlpacaApplication(Protocol):
    async def preview(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome: ...

    async def submit(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome: ...

    async def cancel(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome: ...

    async def get_order(
        self, decision: AlpacaVerifiedDecision
    ) -> AlpacaPaperApplicationOutcome: ...


class AlpacaCryptoPaperAdapter:
    broker = Broker.ALPACA

    def __init__(self, *, application: _AlpacaApplication | None = None) -> None:
        self._application = application or AlpacaPaperOrderApplication()

    async def preview(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._invoke(
            PaperOperation.PREVIEW, intent, self._application.preview
        )

    async def submit(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._invoke(
            PaperOperation.SUBMIT, intent, self._application.submit
        )

    async def cancel(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._invoke(
            PaperOperation.CANCEL, intent, self._application.cancel
        )

    async def get_order(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._invoke(
            PaperOperation.GET_ORDER, intent, self._application.get_order
        )

    async def reconcile(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return self._unsupported(PaperOperation.RECONCILE)

    async def link_native_order(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult:
        return self._unsupported(PaperOperation.LINK_NATIVE_ORDER)

    async def _invoke(
        self,
        operation: PaperOperation,
        intent: VerifiedPaperOrderIntent,
        handler: Callable[
            [AlpacaVerifiedDecision], Awaitable[AlpacaPaperApplicationOutcome]
        ],
    ) -> PaperOperationResult:
        try:
            outcome = await handler(self._decision(intent))
        except Exception as exc:  # noqa: BLE001 — canonical port failure boundary
            return PaperOperationResult(
                operation=operation,
                status=PaperOperationStatus.FAILED,
                reason_code=PaperReasonCode.ADAPTER_UNAVAILABLE,
                venue=self.broker,
                evidence={"error_type": type(exc).__name__},
            )
        return self._result(operation, outcome)

    @staticmethod
    def _decision(intent: VerifiedPaperOrderIntent) -> AlpacaVerifiedDecision:
        assert intent.qty is not None
        assert intent.price is not None
        signal_symbol = intent.symbol.removesuffix("/USD") + "USDT"
        identity_hash = hashlib.sha256(intent.idempotency_key.encode()).hexdigest()
        return AlpacaVerifiedDecision(
            order=AlpacaPaperOrderSpec(
                symbol=intent.symbol,
                side=intent.side,
                order_type="limit",
                qty=intent.qty,
                notional=None,
                time_in_force=intent.time_in_force,  # type: ignore[arg-type]
                limit_price=intent.price,
                asset_class="crypto",
            ),
            decision_id=intent.decision_id,
            signal_symbol=signal_symbol,
            signal_venue="binance_public_spot",
            snapshot_id=intent.market_snapshot_id,
            snapshot_hash=intent.market_snapshot_hash,
            snapshot_as_of=intent.market_snapshot_as_of,
            snapshot_source=intent.market_snapshot_source,
            reference_price=intent.reference_price,
            source_buy_client_order_id=intent.source_buy_client_order_id,
            decision_identity_hash=identity_hash,
        )

    def _unsupported(self, operation: PaperOperation) -> PaperOperationResult:
        return PaperOperationResult.blocked(
            operation=operation,
            venue=self.broker,
            reason_code=PaperReasonCode.UNSUPPORTED_CAPABILITY,
        )

    def _result(
        self,
        operation: PaperOperation,
        outcome: AlpacaPaperApplicationOutcome,
    ) -> PaperOperationResult:
        if outcome.status in {
            "previewed",
            "submitted",
            "replayed",
            "recovered",
            "found",
            "canceled",
        }:
            status = PaperOperationStatus.SUCCEEDED
        elif outcome.status in {
            "rejected",
            "idempotency_in_progress",
            "cancel_requested",
        }:
            status = PaperOperationStatus.BLOCKED
        else:
            status = PaperOperationStatus.FAILED
        evidence: dict[str, object] = dict(outcome.evidence)
        evidence.update(
            {
                "submitted": outcome.submitted,
                "broker_called": outcome.broker_called,
            }
        )
        if outcome.message is not None:
            evidence["message"] = outcome.message
        return PaperOperationResult(
            operation=operation,
            status=status,
            reason_code=outcome.reason_code or PaperReasonCode.OK,
            venue=self.broker,
            native_order_id=outcome.native_order_id,
            native_client_order_id=outcome.native_client_order_id,
            evidence=evidence,
            replayed=outcome.replayed,
        )


__all__ = ["AlpacaCryptoPaperAdapter"]

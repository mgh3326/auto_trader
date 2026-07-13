from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.application import PaperExecutionApplication
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
    PaperReasonCode,
    VerifiedExperimentProvenance,
    VerifiedPaperOrderIntent,
)


def _request(**updates: object) -> PaperOrderRequest:
    data: dict[str, object] = {
        "intent_id": "intent-001",
        "experiment_id": "experiment-001",
        "run_id": "run-001",
        "cohort_id": "cohort-001",
        "strategy_version_id": "strategy-v1",
        "strategy_hash": "sha256:strategy",
        "config_hash": "sha256:config",
        "policy_hash": "sha256:policy",
        "venue": Broker.BINANCE,
        "account_mode": "demo",
        "product": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "order_type": "market",
        "time_in_force": None,
        "qty": None,
        "notional": Decimal("10"),
        "price": None,
        "market_snapshot_id": "snapshot-001",
        "market_snapshot_hash": "sha256:snapshot",
        "market_snapshot_as_of": datetime(2026, 7, 13, 1, 2, tzinfo=UTC),
        "market_snapshot_source": "binance_public_spot",
        "source_buy_reference": None,
    }
    data.update(updates)
    return PaperOrderRequest(**data)


def _verified(request: PaperOrderRequest) -> VerifiedExperimentProvenance:
    return VerifiedExperimentProvenance(
        **request.model_dump(),
        decision_id="decision-001",
        reference_price=Decimal("60000"),
        source_buy_client_order_id=(
            "native-buy-001" if request.side == "sell" else None
        ),
    )


class _Verifier:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]


class _Adapter:
    broker = Broker.BINANCE

    def __init__(self) -> None:
        self.calls: list[tuple[PaperOperation, VerifiedPaperOrderIntent]] = []

    async def _result(
        self, operation: PaperOperation, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult:
        self.calls.append((operation, intent))
        return PaperOperationResult(
            operation=operation,
            status=PaperOperationStatus.SUCCEEDED,
            reason_code=PaperReasonCode.OK,
            venue=intent.venue,
            native_client_order_id=intent.idempotency_key,
        )

    async def preview(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._result(PaperOperation.PREVIEW, intent)

    async def submit(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._result(PaperOperation.SUBMIT, intent)

    async def cancel(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._result(PaperOperation.CANCEL, intent)

    async def get_order(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._result(PaperOperation.GET_ORDER, intent)

    async def reconcile(self, intent: VerifiedPaperOrderIntent) -> PaperOperationResult:
        return await self._result(PaperOperation.RECONCILE, intent)

    async def link_native_order(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult:
        return await self._result(PaperOperation.LINK_NATIVE_ORDER, intent)


class _Registry:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter
        self.resolve_calls = 0

    def resolve(self, broker: Broker) -> _Adapter:
        self.resolve_calls += 1
        assert broker is self.adapter.broker
        return self.adapter


@pytest.mark.asyncio
async def test_missing_verifier_fails_before_registry_or_adapter() -> None:
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(registry=registry, verifier=None)

    result = await application.submit(_request())

    assert result.reason_code is PaperReasonCode.PROVENANCE_VERIFIER_UNAVAILABLE
    assert registry.resolve_calls == 0
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_verifier_exception_fails_before_registry_or_adapter(caplog) -> None:
    adapter = _Adapter()
    registry = _Registry(adapter)
    verifier = _Verifier(RuntimeError("api_secret=must-not-leak"))
    application = PaperExecutionApplication(registry=registry, verifier=verifier)

    result = await application.submit(_request())

    assert result.reason_code is PaperReasonCode.PROVENANCE_VERIFICATION_FAILED
    assert registry.resolve_calls == 0
    assert adapter.calls == []
    assert "paper provenance verifier failed" in caplog.text
    assert "api_secret" not in caplog.text


@pytest.mark.asyncio
async def test_missing_verified_field_fails_before_registry_or_adapter(caplog) -> None:
    request = _request()
    evidence = VerifiedExperimentProvenance.model_construct(
        **{
            key: value
            for key, value in _verified(request).model_dump().items()
            if key != "config_hash"
        }
    )
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(
        registry=registry,
        verifier=_Verifier(evidence),
    )

    result = await application.submit(request)

    assert result.reason_code is PaperReasonCode.PROVENANCE_EVIDENCE_INVALID
    assert registry.resolve_calls == 0
    assert adapter.calls == []
    assert "paper provenance evidence invalid" in caplog.text


@pytest.mark.asyncio
async def test_verified_mismatch_fails_before_registry_or_adapter() -> None:
    request = _request()
    evidence = _verified(request).model_copy(update={"policy_hash": "sha256:other"})
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(
        registry=registry,
        verifier=_Verifier(evidence),
    )

    result = await application.submit(request)

    assert result.reason_code is PaperReasonCode.PROVENANCE_MISMATCH
    assert result.evidence == {"field": "policy_hash"}
    assert registry.resolve_calls == 0
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        (PaperOperation.CANCEL, "cancel"),
        (PaperOperation.RECONCILE, "reconcile"),
    ],
)
async def test_unsupported_operation_does_not_resolve_adapter(
    operation: PaperOperation,
    invoke: str,
) -> None:
    request = _request()
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(
        registry=registry,
        verifier=_Verifier(_verified(request)),
    )

    result = await getattr(application, invoke)(request)

    assert result.operation is operation
    assert result.reason_code is PaperReasonCode.UNSUPPORTED_CAPABILITY
    assert registry.resolve_calls == 0
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"account_mode": "live"},
        {"product": "futures"},
        {"symbol": "SOLUSDT"},
        {"side": "sell", "source_buy_reference": "opaque-buy"},
        {"order_type": "limit", "price": Decimal("60000")},
        {"time_in_force": "gtc"},
        {"qty": Decimal("0.001"), "notional": None},
        {"market_snapshot_source": "caller_supplied"},
    ],
)
async def test_request_outside_capability_surface_does_not_resolve_adapter(
    updates: dict[str, object],
) -> None:
    request = _request(**updates)
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(
        registry=registry,
        verifier=_Verifier(_verified(request)),
    )

    result = await application.submit(request)

    assert result.reason_code is PaperReasonCode.UNSUPPORTED_CAPABILITY
    assert registry.resolve_calls == 0
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_valid_exact_binding_dispatches_server_owned_intent() -> None:
    request = _request()
    verifier = _Verifier(_verified(request))
    adapter = _Adapter()
    registry = _Registry(adapter)
    application = PaperExecutionApplication(registry=registry, verifier=verifier)

    first = await application.submit(request)
    second = await application.submit(request)

    assert first.status is PaperOperationStatus.SUCCEEDED
    assert second.native_client_order_id == first.native_client_order_id
    assert registry.resolve_calls == 2
    assert len(adapter.calls) == 2
    first_intent = adapter.calls[0][1]
    second_intent = adapter.calls[1][1]
    assert first_intent.origin == "experiment"
    assert first_intent.idempotency_key == second_intent.idempotency_key
    assert len(first_intent.idempotency_key) <= 36
    assert "origin" not in request.model_fields_set
    assert "idempotency_key" not in request.model_fields_set


@pytest.mark.asyncio
async def test_valid_preview_and_get_order_dispatch_to_exact_methods() -> None:
    request = _request()
    adapter = _Adapter()
    application = PaperExecutionApplication(
        registry=_Registry(adapter),
        verifier=_Verifier(_verified(request)),
    )

    preview = await application.preview(request)
    get_order = await application.get_order(request)

    assert preview.operation is PaperOperation.PREVIEW
    assert get_order.operation is PaperOperation.GET_ORDER
    assert [call[0] for call in adapter.calls] == [
        PaperOperation.PREVIEW,
        PaperOperation.GET_ORDER,
    ]

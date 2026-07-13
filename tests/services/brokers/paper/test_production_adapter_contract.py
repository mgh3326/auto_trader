from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.alpaca_paper_order_application import AlpacaPaperApplicationOutcome
from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter
from app.services.brokers.binance.paper_adapter import BinanceSpotDemoPaperAdapter
from app.services.brokers.capabilities import (
    PAPER_BROKER_CAPABILITIES,
    Broker,
    PaperBrokerCapabilities,
)
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperReasonCode,
    VerifiedPaperOrderIntent,
)

_OPERATION_SUPPORT_FIELDS = {
    PaperOperation.PREVIEW: "supports_preview",
    PaperOperation.SUBMIT: "supports_submit",
    PaperOperation.CANCEL: "supports_cancel",
    PaperOperation.GET_ORDER: "supports_get_order",
    PaperOperation.RECONCILE: "supports_reconcile",
    PaperOperation.LINK_NATIVE_ORDER: "supports_link_native_order",
}


def _intent(broker: Broker) -> VerifiedPaperOrderIntent:
    if broker is Broker.BINANCE:
        return VerifiedPaperOrderIntent(
            intent_id="intent-binance",
            experiment_id="experiment-1",
            run_id="run-1",
            cohort_id="cohort-1",
            strategy_version_id="strategy-v1",
            strategy_hash="sha256:strategy",
            config_hash="sha256:config",
            policy_hash="sha256:policy",
            venue=broker,
            account_mode="demo",
            product="spot",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            time_in_force=None,
            qty=None,
            notional=Decimal("10"),
            price=None,
            market_snapshot_id="snapshot-1",
            market_snapshot_hash="sha256:snapshot",
            market_snapshot_as_of=datetime(2026, 7, 13, tzinfo=UTC),
            market_snapshot_source="binance_public_spot",
            source_buy_reference=None,
            decision_id="decision-binance",
            reference_price=Decimal("50000"),
            source_buy_client_order_id=None,
            origin="experiment",
            idempotency_key="rob845-binance-contract",
        )
    return VerifiedPaperOrderIntent(
        intent_id="intent-alpaca",
        experiment_id="experiment-1",
        run_id="run-1",
        cohort_id="cohort-1",
        strategy_version_id="strategy-v1",
        strategy_hash="sha256:strategy",
        config_hash="sha256:config",
        policy_hash="sha256:policy",
        venue=broker,
        account_mode="paper",
        product="crypto",
        symbol="BTC/USD",
        side="buy",
        order_type="limit",
        time_in_force="gtc",
        qty=Decimal("0.001"),
        notional=None,
        price=Decimal("50000"),
        market_snapshot_id="snapshot-1",
        market_snapshot_hash="sha256:snapshot",
        market_snapshot_as_of=datetime(2026, 7, 13, tzinfo=UTC),
        market_snapshot_source="binance_public_spot",
        source_buy_reference=None,
        decision_id="decision-alpaca",
        reference_price=Decimal("50000"),
        source_buy_client_order_id=None,
        origin="experiment",
        idempotency_key="rob845-alpaca-contract",
    )


class _AlpacaApplicationSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _call(self, operation: str) -> AlpacaPaperApplicationOutcome:
        self.calls.append(operation)
        return AlpacaPaperApplicationOutcome(
            status="submitted",
            native_client_order_id="alpaca-native-client",
        )

    async def preview(self, decision: object) -> AlpacaPaperApplicationOutcome:
        return await self._call("preview")

    async def submit(self, decision: object) -> AlpacaPaperApplicationOutcome:
        return await self._call("submit")

    async def cancel(self, decision: object) -> AlpacaPaperApplicationOutcome:
        return await self._call("cancel")

    async def get_order(self, decision: object) -> AlpacaPaperApplicationOutcome:
        return await self._call("get_order")


def _dependency_bomb() -> None:
    raise AssertionError("unsupported operation constructed a dependency")


@pytest.mark.parametrize(
    ("adapter_type", "broker"),
    [
        (BinanceSpotDemoPaperAdapter, Broker.BINANCE),
        (AlpacaCryptoPaperAdapter, Broker.ALPACA),
    ],
)
def test_every_advertised_operation_is_an_async_production_adapter_method(
    adapter_type: type[object],
    broker: Broker,
) -> None:
    capability = PAPER_BROKER_CAPABILITIES[broker]

    for operation, support_field in _OPERATION_SUPPORT_FIELDS.items():
        method = getattr(adapter_type, operation.value, None)
        assert callable(method), f"{broker.value} missing {operation.value}"
        assert inspect.iscoroutinefunction(method), (
            f"{broker.value}.{operation.value} must preserve the async port"
        )
        assert capability.supports(operation) is getattr(capability, support_field)


@pytest.mark.asyncio
async def test_alpaca_advertised_methods_delegate_to_guarded_application() -> None:
    capability = PAPER_BROKER_CAPABILITIES[Broker.ALPACA]
    application = _AlpacaApplicationSpy()
    adapter = AlpacaCryptoPaperAdapter(application=application)
    intent = _intent(Broker.ALPACA)

    for operation in PaperOperation:
        if not capability.supports(operation):
            continue
        result = await getattr(adapter, operation.value)(intent)
        assert result.operation is operation
        assert result.status is PaperOperationStatus.SUCCEEDED

    assert application.calls == ["preview", "submit", "cancel", "get_order"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broker", "operation"),
    [
        (Broker.BINANCE, PaperOperation.CANCEL),
        (Broker.BINANCE, PaperOperation.RECONCILE),
        (Broker.ALPACA, PaperOperation.RECONCILE),
        (Broker.ALPACA, PaperOperation.LINK_NATIVE_ORDER),
    ],
)
async def test_false_capability_is_stable_unsupported_without_dependency_access(
    broker: Broker,
    operation: PaperOperation,
) -> None:
    capability: PaperBrokerCapabilities = PAPER_BROKER_CAPABILITIES[broker]
    assert capability.supports(operation) is False

    alpaca_application = _AlpacaApplicationSpy()
    adapter: BinanceSpotDemoPaperAdapter | AlpacaCryptoPaperAdapter
    if broker is Broker.BINANCE:
        adapter = BinanceSpotDemoPaperAdapter(
            session_factory=_dependency_bomb,
            client_factory=_dependency_bomb,
            reference_factory=_dependency_bomb,
            market_data_factory=_dependency_bomb,
        )
    else:
        adapter = AlpacaCryptoPaperAdapter(application=alpaca_application)

    result: PaperOperationResult = await getattr(adapter, operation.value)(
        _intent(broker)
    )

    assert result.operation is operation
    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code is PaperReasonCode.UNSUPPORTED_CAPABILITY
    assert alpaca_application.calls == []

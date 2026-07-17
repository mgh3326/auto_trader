"""ROB-845 Alpaca Crypto Paper canonical adapter contracts."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.alpaca_paper_order_application import AlpacaPaperApplicationOutcome
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationStatus,
    PaperReasonCode,
    VerifiedPaperOrderIntent,
)


def _intent(*, side: str = "buy", symbol: str = "BTC/USD") -> VerifiedPaperOrderIntent:
    is_sell = side == "sell"
    return VerifiedPaperOrderIntent(
        intent_id=f"intent-{side}",
        experiment_id="experiment-1",
        run_id="run-1",
        cohort_id="cohort-1",
        strategy_version_id="strategy-v1",
        strategy_hash="sha256:strategy",
        config_hash="sha256:config",
        policy_hash="sha256:policy",
        venue=Broker.ALPACA,
        account_mode="paper",
        product="crypto",
        symbol=symbol,
        side=side,
        order_type="limit",
        time_in_force="gtc",
        qty=Decimal("0.0005"),
        notional=None,
        price=Decimal("50000"),
        market_snapshot_id="snapshot-1",
        market_snapshot_hash="sha256:snapshot",
        market_snapshot_as_of=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        market_snapshot_source="binance_public_spot",
        source_buy_reference="opaque-buy" if is_sell else None,
        decision_id=f"decision-{side}",
        reference_price=Decimal("50000"),
        source_buy_client_order_id="native-buy-1" if is_sell else None,
        origin="experiment",
        idempotency_key=f"rob845-{side}-idempotency",
    )


class _Application:
    def __init__(self, outcome: AlpacaPaperApplicationOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, object]] = []

    async def preview(self, decision):
        self.calls.append(("preview", decision))
        return self.outcome

    async def submit(self, decision):
        self.calls.append(("submit", decision))
        return self.outcome

    async def cancel(self, decision):
        self.calls.append(("cancel", decision))
        return self.outcome

    async def get_order(self, decision):
        self.calls.append(("get_order", decision))
        return self.outcome


class _SensitiveApplicationFailure(RuntimeError):
    pass


class _RaisingApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def _raise(self, operation: str, decision: object):
        self.calls.append((operation, decision))
        raise _SensitiveApplicationFailure("api_secret=must-not-leak")

    async def preview(self, decision):
        return await self._raise("preview", decision)

    async def submit(self, decision):
        return await self._raise("submit", decision)

    async def cancel(self, decision):
        return await self._raise("cancel", decision)

    async def get_order(self, decision):
        return await self._raise("get_order", decision)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "operation"),
    [
        ("preview", PaperOperation.PREVIEW),
        ("submit", PaperOperation.SUBMIT),
        ("cancel", PaperOperation.CANCEL),
        ("get_order", PaperOperation.GET_ORDER),
    ],
)
async def test_adapter_maps_supported_methods_to_guarded_application(
    method: str, operation: PaperOperation
):
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(
        AlpacaPaperApplicationOutcome(
            status="submitted",
            native_client_order_id="native-client",
            native_order_id="native-order",
            submitted=True,
            evidence={"native": True},
        )
    )
    adapter = AlpacaCryptoPaperAdapter(application=application)

    result = await getattr(adapter, method)(_intent())

    assert adapter.broker is Broker.ALPACA
    assert result.operation is operation
    assert result.status is PaperOperationStatus.SUCCEEDED
    assert result.reason_code is PaperReasonCode.OK
    assert result.native_client_order_id == "native-client"
    assert [call[0] for call in application.calls] == [method]
    decision = application.calls[0][1]
    assert decision.signal_symbol == "BTCUSDT"
    assert decision.signal_venue == "binance_public_spot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "operation"),
    [
        ("preview", PaperOperation.PREVIEW),
        ("submit", PaperOperation.SUBMIT),
        ("cancel", PaperOperation.CANCEL),
        ("get_order", PaperOperation.GET_ORDER),
    ],
)
async def test_adapter_maps_unexpected_application_failure_to_sanitized_result(
    method: str, operation: PaperOperation
):
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _RaisingApplication()
    adapter = AlpacaCryptoPaperAdapter(application=application)

    result = await getattr(adapter, method)(_intent())

    assert result.evidence == {
        "error_type": "_SensitiveApplicationFailure",
    }
    assert application.calls[0][0] == method


@pytest.mark.asyncio
async def test_adapter_fails_closed_before_application_for_unmapped_symbol() -> None:
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(AlpacaPaperApplicationOutcome(status="submitted"))
    adapter = AlpacaCryptoPaperAdapter(application=application)

    result = await adapter.preview(_intent(symbol="SOL/USD"))

    assert result.status is PaperOperationStatus.FAILED
    assert result.reason_code is PaperReasonCode.ADAPTER_UNAVAILABLE
    assert result.evidence == {
        "error_type": "CryptoExecutionMappingError",
    }
    assert application.calls == []


@pytest.mark.asyncio
async def test_adapter_unexpected_failed_outcome_never_reports_ok() -> None:
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(AlpacaPaperApplicationOutcome(status="unexpected"))

    result = await AlpacaCryptoPaperAdapter(application=application).submit(_intent())

    assert result.status is PaperOperationStatus.FAILED
    assert result.reason_code is PaperReasonCode.ADAPTER_UNAVAILABLE


@pytest.mark.asyncio
async def test_adapter_unexpected_failed_outcome_preserves_explicit_reason() -> None:
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(
        AlpacaPaperApplicationOutcome(
            status="unexpected",
            reason_code="native_failure",
        )
    )

    result = await AlpacaCryptoPaperAdapter(application=application).submit(_intent())

    assert result.status is PaperOperationStatus.FAILED
    assert result.reason_code == "native_failure"


@pytest.mark.asyncio
async def test_adapter_pending_cancel_is_blocked_with_non_ok_reason() -> None:
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(
        AlpacaPaperApplicationOutcome(
            status="cancel_requested",
            reason_code="cancel_pending",
            native_client_order_id="native-client",
            native_order_id="native-order",
            broker_called=True,
        )
    )

    result = await AlpacaCryptoPaperAdapter(application=application).cancel(_intent())

    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code == "cancel_pending"
    assert result.reason_code is not PaperReasonCode.OK
    assert result.evidence["broker_called"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "operation"),
    [
        ("reconcile", PaperOperation.RECONCILE),
        ("link_native_order", PaperOperation.LINK_NATIVE_ORDER),
    ],
)
async def test_adapter_unsupported_methods_are_stable_and_side_effect_free(
    method: str, operation: PaperOperation
):
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    application = _Application(AlpacaPaperApplicationOutcome(status="unexpected"))
    adapter = AlpacaCryptoPaperAdapter(application=application)

    result = await getattr(adapter, method)(_intent())

    assert result.operation is operation
    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code is PaperReasonCode.UNSUPPORTED_CAPABILITY
    assert application.calls == []


def test_adapter_source_has_no_raw_submit_or_tooling_import() -> None:
    import app.services.brokers.alpaca.paper_adapter as module

    source = inspect.getsource(module)
    assert ".submit_order(" not in source
    assert "app.mcp_server" not in source
    assert "alpaca_live" not in source


@pytest.mark.asyncio
async def test_adapter_captures_and_sanitizes_http_error_body() -> None:
    from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError
    from app.services.brokers.alpaca.paper_adapter import AlpacaCryptoPaperAdapter

    class _RequestErrorRaisingApplication:
        async def submit(self, decision):
            token = "abcdefghijklmnopqrstuvwxyz1234567890abcd"
            long_msg = "word " * 400
            raise AlpacaPaperRequestError(
                f"HTTP 403: secret: {token} and raw_token: {token} and {long_msg}",
                status_code=403,
            )

    adapter = AlpacaCryptoPaperAdapter(application=_RequestErrorRaisingApplication())
    result = await adapter.submit(_intent())

    assert result.status is PaperOperationStatus.FAILED
    assert result.evidence["error_type"] == "AlpacaPaperRequestError"

    error_body = result.evidence["error_body"]
    assert "secret: [REDACTED]" in error_body
    assert "raw_token: [MASKED_TOKEN]" in error_body
    assert "abcdefghijklmnopqrstuvwxyz1234567890abcd" not in error_body
    assert len(error_body) == 500

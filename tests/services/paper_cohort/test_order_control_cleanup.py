"""Fail-closed cleanup planning over cohort-owned ROB-845 links."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperation,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
    VerifiedExperimentProvenance,
)
from app.services.paper_cohort.order_control import PaperCohortOrderControl


def _request(venue: Broker = Broker.ALPACA) -> PaperOrderRequest:
    alpaca = venue is Broker.ALPACA
    return PaperOrderRequest.model_validate(
        {
            "intent_id": "intent-1",
            "experiment_id": "experiment-1",
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "strategy_version_id": "strategy-v1",
            "strategy_hash": "1" * 64,
            "config_hash": "2" * 64,
            "policy_hash": "3" * 64,
            "venue": venue,
            "account_mode": "paper" if alpaca else "demo",
            "product": "crypto" if alpaca else "spot",
            "symbol": "BTC/USD" if alpaca else "BTCUSDT",
            "side": "buy",
            "order_type": "limit" if alpaca else "market",
            "time_in_force": "gtc" if alpaca else None,
            "qty": Decimal("0.001") if alpaca else None,
            "notional": None if alpaca else Decimal("25"),
            "price": Decimal("50000") if alpaca else None,
            "market_snapshot_id": "snapshot-1",
            "market_snapshot_hash": "4" * 64,
            "market_snapshot_as_of": datetime(2026, 7, 14, tzinfo=UTC),
            "market_snapshot_source": "binance_public_spot",
        }
    )


def _evidence(request: PaperOrderRequest) -> VerifiedExperimentProvenance:
    return VerifiedExperimentProvenance(
        **request.model_dump(),
        decision_id="decision-1",
        reference_price=Decimal("50000"),
    )


class FakeApplication:
    def __init__(self, *results: PaperOperationResult) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.requests: list[PaperOrderRequest] = []

    async def get_order(self, request: PaperOrderRequest) -> PaperOperationResult:
        self.calls.append("get_order")
        self.requests.append(request)
        return self.results.pop(0)

    async def cancel(self, request: PaperOrderRequest) -> PaperOperationResult:
        self.calls.append("cancel")
        self.requests.append(request)
        return self.results.pop(0)

    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult:
        self.calls.append("submit")
        self.requests.append(request)
        return self.results.pop(0)


def _operation(
    operation: PaperOperation,
    *,
    status: PaperOperationStatus = PaperOperationStatus.SUCCEEDED,
    reason_code: str = "ok",
    evidence: dict[str, object] | None = None,
) -> PaperOperationResult:
    return PaperOperationResult(
        operation=operation,
        status=status,
        reason_code=reason_code,
        venue=Broker.ALPACA,
        evidence=evidence or {},
    )


def _control(
    application: FakeApplication,
    request: PaperOrderRequest | None = None,
) -> PaperCohortOrderControl:
    current = request or _request()
    control = PaperCohortOrderControl(
        None,  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        application_factory=lambda _verifier: application,
        native_resolver=object(),  # type: ignore[arg-type]
    )
    control._owned_request = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            current,
            SimpleNamespace(
                id=7,
                venue=current.venue.value,
                client_order_id="client-order-1",
            ),
            _evidence(current),
        )
    )
    return control


@pytest.mark.asyncio
async def test_unknown_native_status_never_reaches_cancel_or_close() -> None:
    application = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "provider-new-status"},
        )
    )
    control = _control(application)

    cleanup = getattr(control, "cleanup", None)

    assert cleanup is not None
    result = await cleanup("cohort-1", 7)
    assert result.status == "pending"
    assert result.action == "none"
    assert result.reason_code == "native_status_unknown"
    assert application.calls == ["get_order"]


@pytest.mark.asyncio
async def test_open_order_cancels_first_and_defers_close_until_terminal_proof() -> None:
    application = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "partially_filled", "filled_qty": "0.0002"},
        ),
        _operation(
            PaperOperation.CANCEL,
            status=PaperOperationStatus.BLOCKED,
            reason_code="cancel_pending",
        ),
    )

    result = await _control(application).cleanup("cohort-1", 7)

    assert result.status == "pending"
    assert result.action == "cancel"
    assert result.reason_code == "cancel_pending"
    assert application.calls == ["get_order", "cancel"]


@pytest.mark.asyncio
async def test_partial_fill_is_canceled_reread_then_closed_at_exact_filled_qty() -> (
    None
):
    application = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "partially_filled", "filled_qty": "0.0002"},
        ),
        _operation(PaperOperation.CANCEL),
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "canceled", "filled_qty": "0.0003"},
        ),
        _operation(PaperOperation.SUBMIT),
    )

    result = await _control(application).cleanup("cohort-1", 7)

    assert result.status == "complete"
    assert result.action == "close"
    assert application.calls == ["get_order", "cancel", "get_order", "submit"]
    close_request = application.requests[-1]
    assert close_request.side == "sell"
    assert close_request.qty == Decimal("0.0003")
    assert close_request.intent_id == "intent-1:close:7"
    assert close_request.source_buy_reference == "client-order-1"


@pytest.mark.asyncio
async def test_filled_native_order_can_close_without_cancel() -> None:
    application = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "filled", "filled_qty": None},
        ),
        _operation(PaperOperation.SUBMIT),
    )

    result = await _control(application).cleanup("cohort-1", 7)

    assert result.status == "complete"
    assert result.action == "close"
    assert application.calls == ["get_order", "submit"]
    # A canonical full-fill status proves the persisted buy's entire requested
    # quantity filled even if the provider omitted its redundant filled_qty.
    assert application.requests[-1].qty == _request().qty


@pytest.mark.asyncio
async def test_terminal_unfilled_order_requires_explicit_zero_fill_proof() -> None:
    unknown = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "canceled", "filled_qty": None},
        )
    )
    proven_zero = FakeApplication(
        _operation(
            PaperOperation.GET_ORDER,
            evidence={"order_status": "canceled", "filled_qty": "0"},
        )
    )

    pending = await _control(unknown).cleanup("cohort-1", 7)
    complete = await _control(proven_zero).cleanup("cohort-1", 7)

    assert (pending.status, pending.reason_code) == (
        "pending",
        "native_filled_quantity_unknown",
    )
    assert (complete.status, complete.reason_code) == (
        "complete",
        "native_order_terminal",
    )
    assert unknown.calls == proven_zero.calls == ["get_order"]


@pytest.mark.asyncio
async def test_unsupported_venue_never_reaches_native_application() -> None:
    application = FakeApplication()

    result = await _control(application, _request(Broker.BINANCE)).cleanup(
        "cohort-1", 7
    )

    assert result.status == "manual_required"
    assert result.action == "none"
    assert result.reason_code == "unsupported_capability"
    assert application.calls == []

"""Tests for the ROB-95 operator-gated KIS mock market-open pilot."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.kis_mock_market_open_pilot import (
    KisMockMarketOpenPilotRequest,
    classify_kis_mock_market_open_report,
    expected_kis_mock_submit_approval_text,
    run_kis_mock_market_open_pilot,
)


class FakeKisMockPlaceOrder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "ok": True,
            "account_mode": kwargs.get("account_mode"),
            "dry_run": kwargs.get("dry_run"),
            "order_id": "mock-order-1",
            "fill_recorded": False,
            "journal_created": False,
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_mode_is_read_only_and_reports_guard_configuration() -> None:
    route = FakeKisMockPlaceOrder()

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="readiness",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: False,
        readiness_probe=lambda: {"mock_config_present": True, "quote_checked": True},
    )

    assert result.status == "ready"
    assert result.mode == "readiness"
    assert result.dry_run is None
    assert result.tool_name == "kis_mock_place_order"
    assert result.account_mode == "kis_mock"
    assert result.safety_checks["typed_kis_mock_route_only"] is True
    assert result.readiness == {"mock_config_present": True, "quote_checked": True}
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_calls_only_typed_kis_mock_place_order_with_forced_dry_run_true() -> (
    None
):
    route = FakeKisMockPlaceOrder()

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="dry-run",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: False,
    )

    assert result.status == "submitted"
    assert result.dry_run is True
    assert result.response == {
        "ok": True,
        "account_mode": "kis_mock",
        "dry_run": True,
        "order_id": "mock-order-1",
        "fill_recorded": False,
        "journal_created": False,
    }
    assert route.calls == [
        {
            "symbol": "005930",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "price": 229500,
            "dry_run": True,
            "account_mode": "kis_mock",
            "reason": "ROB-95 KIS mock market-open pilot dry-run",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_mock_rejects_missing_exact_approval_text_without_calling_route() -> (
    None
):
    route = FakeKisMockPlaceOrder()

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="submit-mock",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: True,
    )

    assert result.status == "blocked"
    assert result.blocking_reasons == ["approval_text_mismatch"]
    assert result.expected_approval_text == (
        "ROB-95 KIS mock 승인: 005930 매수 1주 지정가 229500원 "
        "account_mode=kis_mock dry_run=False 정규장 모의투자 제출 승인"
    )
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_mock_rejects_whitespace_variation_in_approval_text() -> None:
    route = FakeKisMockPlaceOrder()
    approval = expected_kis_mock_submit_approval_text(
        symbol="005930", side="buy", quantity=1, price=Decimal("229500")
    )

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="submit-mock",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
            approval_text=f" {approval} ",
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: True,
    )

    assert result.status == "blocked"
    assert result.blocking_reasons == ["approval_text_mismatch"]
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_mock_rejects_non_regular_session_even_with_exact_approval() -> (
    None
):
    route = FakeKisMockPlaceOrder()
    approval = expected_kis_mock_submit_approval_text(
        symbol="005930", side="buy", quantity=1, price=Decimal("229500")
    )

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="submit-mock",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
            approval_text=approval,
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: False,
    )

    assert result.status == "blocked"
    assert result.blocking_reasons == ["regular_session_closed"]
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_mock_rejects_quantity_above_one_by_default() -> None:
    route = FakeKisMockPlaceOrder()
    approval = expected_kis_mock_submit_approval_text(
        symbol="005930", side="buy", quantity=2, price=Decimal("229500")
    )

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="submit-mock",
            symbol="005930",
            side="buy",
            quantity=2,
            price=Decimal("229500"),
            approval_text=approval,
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: True,
    )

    assert result.status == "blocked"
    assert result.blocking_reasons == ["quantity_exceeds_default_smoke_limit"]
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "symbol",
        "side",
        "quantity",
        "price",
        "account_mode",
        "tool_name",
        "expected_reason",
    ),
    [
        (
            "AAPL",
            "buy",
            1,
            Decimal("229500"),
            "kis_mock",
            "kis_mock_place_order",
            "unsupported_kr_equity_symbol",
        ),
        (
            "005930",
            "hold",
            1,
            Decimal("229500"),
            "kis_mock",
            "kis_mock_place_order",
            "unsupported_side",
        ),
        (
            "005930",
            "buy",
            1,
            Decimal("0"),
            "kis_mock",
            "kis_mock_place_order",
            "invalid_limit_price",
        ),
        (
            "005930",
            "buy",
            1,
            Decimal("229500"),
            "kis_live",
            "kis_mock_place_order",
            "invalid_account_mode",
        ),
        (
            "005930",
            "buy",
            1,
            Decimal("229500"),
            "kis_mock",
            "place_order",
            "invalid_tool_name",
        ),
        (
            "005930",
            "buy",
            1,
            Decimal("229500"),
            "kis_mock",
            "kis_live_place_order",
            "invalid_tool_name",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runner_rejects_unsafe_request_shape_before_any_route_call(
    symbol: str,
    side: str,
    quantity: int,
    price: Decimal,
    account_mode: str,
    tool_name: str,
    expected_reason: str,
) -> None:
    route = FakeKisMockPlaceOrder()

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="dry-run",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            account_mode=account_mode,
            tool_name=tool_name,
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: True,
    )

    assert result.status == "blocked"
    assert expected_reason in result.blocking_reasons
    assert route.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_mock_with_exact_approval_calls_typed_route_with_dry_run_false() -> (
    None
):
    route = FakeKisMockPlaceOrder()
    approval = expected_kis_mock_submit_approval_text(
        symbol="005930", side="buy", quantity=1, price=Decimal("229500")
    )

    result = await run_kis_mock_market_open_pilot(
        KisMockMarketOpenPilotRequest(
            mode="submit-mock",
            symbol="005930",
            side="buy",
            quantity=1,
            price=Decimal("229500"),
            approval_text=approval,
        ),
        kis_mock_place_order=route,
        is_regular_session=lambda: True,
    )

    assert result.status == "submitted"
    assert result.dry_run is False
    assert result.report_status == "accepted_but_fill_unknown"
    assert route.calls == [
        {
            "symbol": "005930",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "price": 229500,
            "dry_run": False,
            "account_mode": "kis_mock",
            "reason": "ROB-95 KIS mock market-open pilot exact-approved mock submit",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_classification_separates_acceptance_from_inferred_fill() -> None:
    assert (
        classify_kis_mock_market_open_report(
            response={"ok": True, "fill_recorded": False, "journal_created": False},
            holdings_delta_qty=0,
            cash_delta_krw=0,
            order_history_supported=False,
        )
        == "accepted_but_fill_unknown"
    )
    assert (
        classify_kis_mock_market_open_report(
            response={"ok": True, "fill_recorded": False, "journal_created": False},
            holdings_delta_qty=1,
            cash_delta_krw=-229500,
            order_history_supported=True,
        )
        == "filled_inferred"
    )

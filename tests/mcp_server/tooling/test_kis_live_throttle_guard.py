"""KIS live gateway-throttle failure-surfacing tests."""

from __future__ import annotations

from inspect import getsource
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.mcp_server.tooling.order_execution as oe
from app.models.review import OrderSendIntent
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.order_throttle import KISGatewayThrottleRejection


@pytest_asyncio.fixture(autouse=True)
async def _clean_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


def _execute_kwargs(*, idempotency_key: str | None = "rob1250-guard") -> dict:
    return {
        "normalized_symbol": "005930",
        "side": "sell",
        "order_type": "limit",
        "order_quantity": 1,
        "price": 70000,
        "market_type": "equity_kr",
        "current_price": 70000,
        "avg_price": 0.0,
        "dry_run_result": {"price": 70000, "quantity": 1, "estimated_value": 70000},
        "order_amount": 70000,
        "reason": "test",
        "exit_reason": None,
        "thesis": None,
        "strategy": None,
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": False,
        "idempotency_key": idempotency_key,
    }


def _throttle() -> KISGatewayThrottleRejection:
    return KISGatewayThrottleRejection(
        message_code="EGW00201",
        message="gateway throttle",
        http_status=200,
        broker_order_id=None,
    )


@pytest.mark.asyncio
async def test_proven_not_delivered_surfaces_after_exactly_one_post(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
):
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(oe, "_kis_live_order_ledger_entry_exists", lookup)
    executed = AsyncMock(side_effect=_throttle())
    monkeypatch.setattr(oe, "_execute_order", executed)

    with pytest.raises(oe.KISGatewayThrottleSubmissionFailure) as raised:
        await oe._execute_and_record(**_execute_kwargs())

    failure = raised.value
    assert executed.await_count == 1
    assert failure.not_delivered is True
    assert failure.error_code == "kis_gateway_throttle_not_delivered"
    assert failure.submit_failure_detail()["post_attempts"] == 1
    surfaced = oe._augment_error_for_unknown_outcome(
        {"success": False, "error": "base"},
        failure,
        market_type="equity_kr",
        is_mock=False,
    )
    assert surfaced["error_code"] == "kis_gateway_throttle_not_delivered"
    assert surfaced["submit_failure"]["post_attempts"] == 1
    assert surfaced.get("outcome_unknown") is not True
    lookup.assert_awaited_once_with(
        idempotency_key="rob1250-guard",
        market_type="equity_kr",
    )
    intent = await db_session.scalar(
        select(OrderSendIntent).where(
            OrderSendIntent.account_scope == "kis_live",
            OrderSendIntent.idempotency_key == "rob1250-guard",
        )
    )
    assert intent is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_entry_present", [True, None])
async def test_ambiguous_ledger_evidence_surfaces_without_another_post(
    monkeypatch: pytest.MonkeyPatch,
    ledger_entry_present: bool | None,
):
    lookup = AsyncMock(return_value=ledger_entry_present)
    monkeypatch.setattr(oe, "_kis_live_order_ledger_entry_exists", lookup)
    executed = AsyncMock(side_effect=_throttle())
    monkeypatch.setattr(oe, "_execute_order", executed)

    with pytest.raises(oe.KISGatewayThrottleSubmissionFailure) as raised:
        await oe._execute_and_record(**_execute_kwargs())

    assert executed.await_count == 1
    assert raised.value.not_delivered is False
    assert raised.value.error_code == "kis_gateway_throttle_delivery_ambiguous"
    surfaced = oe._augment_error_for_unknown_outcome(
        {"success": False, "error": "base"},
        raised.value,
        market_type="equity_kr",
        is_mock=False,
    )
    assert surfaced["error_code"] == "kis_gateway_throttle_delivery_ambiguous"
    assert surfaced["outcome_unknown"] is True
    assert surfaced["submit_failure"]["ledger_entry_present"] is ledger_entry_present
    assert surfaced["submit_failure"]["post_attempts"] == 1


@pytest.mark.asyncio
async def test_missing_idempotency_key_surfaces_as_ambiguous_after_one_post(
    monkeypatch: pytest.MonkeyPatch,
):
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(oe, "_kis_live_order_ledger_entry_exists", lookup)
    executed = AsyncMock(side_effect=_throttle())
    monkeypatch.setattr(oe, "_execute_order", executed)

    with pytest.raises(oe.KISGatewayThrottleSubmissionFailure) as raised:
        await oe._execute_and_record(**_execute_kwargs(idempotency_key=None))

    assert executed.await_count == 1
    lookup.assert_not_awaited()
    assert raised.value.not_delivered is False


@pytest.mark.asyncio
async def test_us_executor_threads_outcome_to_capable_facade_without_account_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict = {}

    class _FacadeWithoutAccountMarker:
        async def buy_overseas_stock(self, **kwargs):
            seen.update(kwargs)
            return {"rt_cd": "0", "odno": "US-1250"}

    monkeypatch.setattr(
        oe,
        "_create_kis_client",
        lambda **_kwargs: _FacadeWithoutAccountMarker(),
    )
    monkeypatch.setattr(oe, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD"))
    tracker = oe.OrderSendOutcomeTracker()

    result = await oe._execute_us_order(
        "QQQM",
        "buy",
        1,
        100.0,
        send_outcome=tracker,
    )

    assert result["odno"] == "US-1250"
    assert seen["send_outcome"] is tracker


@pytest.mark.asyncio
async def test_kr_executor_keeps_legacy_facade_signature_unchanged(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict = {}

    class _LegacyFacade:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            seen.update(
                stock_code=stock_code,
                order_type=order_type,
                quantity=quantity,
                price=price,
            )
            return {"rt_cd": "0", "odno": "KR-1250"}

    monkeypatch.setattr(oe, "_create_kis_client", lambda **_kwargs: _LegacyFacade())

    result = await oe._execute_kr_order(
        "005930",
        "sell",
        "limit",
        1,
        70000.0,
        send_outcome=oe.OrderSendOutcomeTracker(),
    )

    assert result["odno"] == "KR-1250"
    assert seen == {
        "stock_code": "005930",
        "order_type": "sell",
        "quantity": 1,
        "price": 70000,
    }


def test_live_order_dispatch_contains_no_order_pacer():
    """Guard the §77 scope split at the common live dispatch boundary."""
    source = getsource(BaseKISClient._dispatch_rate_limited_with_headers)
    assert "live_order_pacer" not in source
    assert "live_order_scope" not in source

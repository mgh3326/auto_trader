"""Evidence-gated KIS live gateway-throttle retry tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.mcp_server.tooling.order_execution as oe
from app.models.review import OrderSendIntent
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


def _patch_accepted_ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    accepted_ledger = AsyncMock(return_value={"success": True, "ledger_id": 1})
    monkeypatch.setattr(
        "app.mcp_server.tooling.kis_live_ledger._record_kis_live_order",
        accepted_ledger,
    )
    monkeypatch.setattr(oe, "_record_order_history", AsyncMock(return_value=None))
    return accepted_ledger


@pytest.mark.asyncio
async def test_proven_not_delivered_retries_once_with_same_reserved_intent(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
):
    _patch_accepted_ledger(monkeypatch)
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(oe, "_kis_live_order_ledger_entry_exists", lookup)
    slept = AsyncMock()
    monkeypatch.setattr(oe.asyncio, "sleep", slept)

    calls: list[dict] = []

    async def _execute(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _throttle()
        return {"rt_cd": "0", "odno": "KIS-1250", "msg": "ok"}

    monkeypatch.setattr(oe, "_execute_order", _execute)

    result = await oe._execute_and_record(**_execute_kwargs())

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[0]["send_outcome"] is calls[1]["send_outcome"]
    assert calls[0]["send_outcome"] is not None
    lookup.assert_awaited_once_with(
        idempotency_key="rob1250-guard",
        market_type="equity_kr",
    )
    slept.assert_awaited_once_with(0.25)
    intent = await db_session.scalar(
        select(OrderSendIntent).where(
            OrderSendIntent.account_scope == "kis_live",
            OrderSendIntent.idempotency_key == "rob1250-guard",
        )
    )
    assert intent is not None


@pytest.mark.asyncio
async def test_second_throttle_stops_after_exactly_one_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_accepted_ledger(monkeypatch)
    monkeypatch.setattr(
        oe,
        "_kis_live_order_ledger_entry_exists",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(oe.asyncio, "sleep", AsyncMock())
    calls = 0

    async def _execute(**_kwargs):
        nonlocal calls
        calls += 1
        raise _throttle()

    monkeypatch.setattr(oe, "_execute_order", _execute)

    with pytest.raises(oe.KISGatewayThrottleSubmissionFailure) as raised:
        await oe._execute_and_record(**_execute_kwargs())

    failure = raised.value
    assert calls == 2
    assert failure.not_delivered is True
    assert failure.retry_count == 1
    assert failure.error_code == "kis_gateway_throttle_not_delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_entry_present", [True, None])
async def test_ambiguous_ledger_evidence_never_retries(
    monkeypatch: pytest.MonkeyPatch,
    ledger_entry_present: bool | None,
):
    _patch_accepted_ledger(monkeypatch)
    lookup = AsyncMock(return_value=ledger_entry_present)
    monkeypatch.setattr(oe, "_kis_live_order_ledger_entry_exists", lookup)
    executed = AsyncMock(side_effect=_throttle())
    monkeypatch.setattr(oe, "_execute_order", executed)
    sleep = AsyncMock()
    monkeypatch.setattr(oe.asyncio, "sleep", sleep)

    with pytest.raises(oe.KISGatewayThrottleSubmissionFailure) as raised:
        await oe._execute_and_record(**_execute_kwargs())

    assert executed.await_count == 1
    sleep.assert_not_awaited()
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


@pytest.mark.asyncio
async def test_missing_idempotency_key_never_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_accepted_ledger(monkeypatch)
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
async def test_us_executor_threads_outcome_tracker_to_concrete_kis_facade(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict = {}

    class _ConcreteLikeKIS:
        _is_mock_client = False

        async def buy_overseas_stock(self, **kwargs):
            seen.update(kwargs)
            return {"rt_cd": "0", "odno": "US-1250"}

    monkeypatch.setattr(oe, "_create_kis_client", lambda **_kwargs: _ConcreteLikeKIS())
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

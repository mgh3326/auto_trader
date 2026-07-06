import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

import app.mcp_server.tooling.kis_mock_ledger as kis_mock_ledger
import app.mcp_server.tooling.order_execution as oe
from app.models.review import OrderSendIntent
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_order_send_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()
    yield
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


def _execute_kwargs(*, key: str, is_mock: bool) -> dict:
    return {
        "normalized_symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "order_quantity": 2,
        "price": 70000,
        "market_type": "equity_kr",
        "current_price": 70000,
        "avg_price": 0.0,
        "dry_run_result": {
            "price": 70000,
            "quantity": 2,
            "estimated_value": 140000,
        },
        "order_amount": 140000,
        "reason": "ROB-750 mirror retry regression",
        "exit_reason": None,
        "thesis": "counterfactual mirror",
        "strategy": "mirror_counterfactual",
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": "source_bucket=place_original",
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": is_mock,
        "correlation_id": key if is_mock else None,
        "report_item_uuid": None,
        "approval_hash_digest": None,
        "idempotency_key": None if is_mock else key,
        "mirror_cohort": "mock_counterfactual" if is_mock else None,
        "mirror_source_bucket": "place_original" if is_mock else None,
    }


def _stub_kis_mock_baseline(monkeypatch):
    async def fake_baseline_qty(**kwargs):
        return None

    monkeypatch.setattr(
        kis_mock_ledger,
        "_fetch_kis_mock_baseline_qty",
        fake_baseline_qty,
    )


@pytest.mark.asyncio
async def test_mock_mirror_intent_released_after_send_request_error(
    monkeypatch,
    db_session,
):
    _stub_kis_mock_baseline(monkeypatch)
    key = "mirror:rob750-request-error"

    async def fail_send(**kwargs):
        raise httpx.ConnectError("temporary mock broker outage")

    monkeypatch.setattr(oe, "_execute_order", fail_send)

    with pytest.raises(oe.OrderSendOutcomeUnknown):
        await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=True))

    # A retry must be able to reserve the same mirror key again; before ROB-750
    # this raised DuplicateOrderIntent because the first reservation was permanent.
    rid = await OrderSendIntentService(db_session).reserve(
        account_scope="kis_mock",
        idempotency_key=key,
    )
    assert isinstance(rid, int)


@pytest.mark.asyncio
async def test_live_intent_is_not_released_after_unknown_send_outcome(
    monkeypatch,
    db_session,
):
    key = "rob750-live-unknown"

    async def fail_send(**kwargs):
        raise httpx.ReadTimeout("live outcome unknown")

    monkeypatch.setattr(oe, "_execute_order", fail_send)

    with pytest.raises(oe.OrderSendOutcomeUnknown):
        await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=False))

    with pytest.raises(DuplicateOrderIntent):
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_live",
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_mock_mirror_duplicate_message_does_not_claim_next_day_retry(
    monkeypatch,
    db_session,
):
    _stub_kis_mock_baseline(monkeypatch)
    key = "mirror:rob750-duplicate-message"
    sent = {"count": 0}

    await OrderSendIntentService(db_session).reserve(
        account_scope="kis_mock",
        idempotency_key=key,
    )

    async def fake_send(**kwargs):
        sent["count"] += 1
        return {"odno": "SHOULD-NOT-SEND", "rt_cd": "0", "msg": "ok"}

    monkeypatch.setattr(oe, "_execute_order", fake_send)

    result = await oe._execute_and_record(**_execute_kwargs(key=key, is_mock=True))

    assert result["success"] is False
    assert "미러" in result["error"]
    assert "익일" not in result["error"]
    assert sent["count"] == 0
# tests/services/test_order_send_intent_service.py
import pytest

from app.models.review import OrderSendIntent  # noqa: F401 (ensures table registered)
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)


@pytest.mark.asyncio
async def test_reserve_inserts_then_blocks_duplicate(db_session):
    svc = OrderSendIntentService(db_session)
    rid = await svc.reserve(
        account_scope="kis_live", idempotency_key="p6a-abc", symbol="005930", side="buy"
    )
    assert isinstance(rid, int)

    with pytest.raises(DuplicateOrderIntent):
        await svc.reserve(
            account_scope="kis_live",
            idempotency_key="p6a-abc",
            symbol="005930",
            side="buy",
        )


@pytest.mark.asyncio
async def test_reserve_allows_distinct_key(db_session):
    svc = OrderSendIntentService(db_session)
    await svc.reserve(account_scope="kis_live", idempotency_key="p6a-day1")
    # a different key (e.g. next trading-day salt) is allowed
    rid = await svc.reserve(account_scope="kis_live", idempotency_key="p6a-day2")
    assert isinstance(rid, int)

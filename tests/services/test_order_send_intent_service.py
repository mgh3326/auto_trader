# tests/services/test_order_send_intent_service.py
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import OrderSendIntent
from app.services.order_send_intent_service import (
    DuplicateOrderIntent,
    OrderSendIntentService,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


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


@pytest.mark.asyncio
async def test_release_deletes_matching_intent_and_allows_re_reserve(db_session):
    svc = OrderSendIntentService(db_session)
    key = "mirror:rob750-release"

    await svc.reserve(account_scope="kis_mock", idempotency_key=key)
    deleted = await svc.release(account_scope="kis_mock", idempotency_key=key)

    assert deleted == 1
    rid = await svc.reserve(account_scope="kis_mock", idempotency_key=key)
    assert isinstance(rid, int)


@pytest.mark.asyncio
async def test_release_is_idempotent_for_missing_intent(db_session):
    svc = OrderSendIntentService(db_session)

    deleted = await svc.release(
        account_scope="kis_mock",
        idempotency_key="mirror:missing",
    )

    assert deleted == 0


@pytest.mark.asyncio
async def test_release_is_scoped_by_account_scope(db_session):
    svc = OrderSendIntentService(db_session)
    key = "mirror:same-key"

    await svc.reserve(account_scope="kis_live", idempotency_key=key)
    deleted = await svc.release(account_scope="kis_mock", idempotency_key=key)

    assert deleted == 0
    with pytest.raises(DuplicateOrderIntent):
        await svc.reserve(account_scope="kis_live", idempotency_key=key)

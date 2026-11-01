"""The real store against the run-owned test database (runs in CI).

Proves what the memory store cannot: the row commits before the forward, DB
constraints hold, and concurrent children cannot spend one cap twice or send one
key twice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal, engine
from app.models.base import Base
from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.schemas.execution_intent import (
    CancelChild,
    ExecutionIntentV1,
    SubmitChild,
)
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.store import (
    IntentConflict,
    IntentNotStorable,
    SqlAlchemyGatewayStore,
)
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    FakeBroker,
    FakeKillSwitch,
)

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration

_TABLES = [ExecutionIntent.__table__, ExecutionChildRequest.__table__]


def _now() -> datetime:
    return datetime.now(UTC)


def _intent(**overrides: Any) -> ExecutionIntentV1:
    values: dict[str, Any] = {
        "schema_version": 1,
        "intent_id": uuid4(),
        "strategy": "s3-test",
        "account": "kis_mock",
        "venue": "krx",
        "symbol": "005930",
        "side": "buy",
        "max_quantity": 10,
        "max_notional_krw": 1_000_000,
        "limit_price_krw": 70_000,
        "max_slippage_bps": 100,
        "expires_at": _now() + timedelta(hours=1),
        "inventory": {
            "position_at_authorization": 0,
            "min_position_after": 0,
            "max_position_after": 10,
        },
        "policy_version": "policy-1",
        "authorization_ref": "hk:2528",
    }
    return ExecutionIntentV1.model_validate({**values, **overrides})


def _submit(intent: ExecutionIntentV1, key: str, quantity: int = 1) -> SubmitChild:
    return SubmitChild(
        action="submit",
        intent_id=intent.intent_id,
        idempotency_key=key,
        quantity=quantity,
        limit_price_krw=70_000,
    )


@pytest_asyncio.fixture(autouse=True)
async def _schema_and_cleanup() -> Any:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES, checkfirst=True)
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ExecutionChildRequest))
        await session.execute(delete(ExecutionIntent))
        await session.commit()


@pytest.fixture
def store() -> SqlAlchemyGatewayStore:
    return SqlAlchemyGatewayStore(AsyncSessionLocal)


def _gateway(store: SqlAlchemyGatewayStore, broker: Any) -> IntentGateway:
    return IntentGateway(
        store=store,
        forwarders={"kis_mock": broker},
        kill_switch=FakeKillSwitch(),
        environment=ENABLED,
    )


async def _rows(intent: ExecutionIntentV1) -> list[ExecutionChildRequest]:
    async with AsyncSessionLocal() as session:
        result = await session.scalars(
            select(ExecutionChildRequest)
            .where(ExecutionChildRequest.intent_id == intent.intent_id)
            .order_by(ExecutionChildRequest.id)
        )
        return list(result)


@pytest.mark.asyncio
async def test_store_intent_is_idempotent_and_refuses_changed_contents(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    await store.store_intent(intent, now=_now())
    changed = intent.model_copy(update={"max_quantity": 9})
    with pytest.raises(IntentConflict):
        await store.store_intent(changed, now=_now())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_in", [timedelta(seconds=-1), timedelta(0), timedelta(hours=25)]
)
async def test_store_intent_refuses_expired_or_standing_authorization(
    store: SqlAlchemyGatewayStore, expires_in: timedelta
) -> None:
    now = _now()
    with pytest.raises(IntentNotStorable):
        await store.store_intent(_intent(expires_at=now + expires_in), now=now)


@pytest.mark.asyncio
async def test_row_is_committed_as_sending_before_the_forward(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    seen: list[tuple[str, Any]] = []

    class Observing(FakeBroker):
        async def submit(self, intent_snapshot: Any, request: Any) -> Any:
            # A separate session sees only committed rows.
            rows = await _rows(intent)
            seen.extend((row.state, row.finished_at) for row in rows)
            return await super().submit(intent_snapshot, request)

    outcome = await _gateway(store, Observing()).handle(
        _submit(intent, "child-0001"), confirm=True
    )
    assert seen == [("sending", None)]
    assert outcome.status == "accepted"
    row = (await _rows(intent))[0]
    assert (row.state, row.broker_order_id) == ("accepted", "1001")
    assert row.result == {"success": True}
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_replay_returns_the_stored_first_result(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    broker = FakeBroker()
    gateway = _gateway(store, broker)
    request = _submit(intent, "child-0001")
    first = await gateway.handle(request, confirm=True)
    second = await gateway.handle(request, confirm=True)
    assert broker.calls == [("submit", "child-0001")]
    assert (second.replayed, second.result) == (True, first.result)
    assert second.broker_order_id == first.broker_order_id


@pytest.mark.asyncio
async def test_concurrent_same_key_sends_once(store: SqlAlchemyGatewayStore) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    broker = FakeBroker()
    gateway = _gateway(store, broker)
    request = _submit(intent, "child-0001")
    outcomes = await asyncio.gather(
        *(gateway.handle(request, confirm=True) for _ in range(8))
    )
    assert broker.calls == [("submit", "child-0001")]
    assert sum(outcome.replayed for outcome in outcomes) == 7
    assert len(await _rows(intent)) == 1


@pytest.mark.asyncio
async def test_concurrent_children_cannot_spend_one_cap_twice(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent(max_quantity=3)
    await store.store_intent(intent, now=_now())
    broker = FakeBroker()
    gateway = _gateway(store, broker)
    await asyncio.gather(
        *(
            gateway.handle(_submit(intent, f"child-{index:04d}"), confirm=True)
            for index in range(8)
        )
    )
    assert len(broker.calls) == 3
    states = [row.state for row in await _rows(intent)]
    assert states.count("accepted") == 3
    assert states.count("refused") == 5


@pytest.mark.asyncio
async def test_key_used_under_another_intent_is_a_conflict(
    store: SqlAlchemyGatewayStore,
) -> None:
    first_intent, second_intent = _intent(), _intent()
    await store.store_intent(first_intent, now=_now())
    await store.store_intent(second_intent, now=_now())
    broker = FakeBroker()
    gateway = _gateway(store, broker)
    await gateway.handle(_submit(first_intent, "child-0001"), confirm=True)
    outcome = await gateway.handle(_submit(second_intent, "child-0001"), confirm=True)
    assert outcome.status == "conflict"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_finish_cannot_rewrite_a_finished_row(
    store: SqlAlchemyGatewayStore,
) -> None:
    from app.services.execution_intent_gateway.decisions import ChildState

    intent = _intent()
    await store.store_intent(intent, now=_now())
    await _gateway(store, FakeBroker()).handle(
        _submit(intent, "child-0001"), confirm=True
    )
    row = (await _rows(intent))[0]
    assert (
        await store.finish(
            row.id,
            state=ChildState.UNCERTAIN,
            broker_order_id=None,
            result={},
            now=_now(),
        )
        is False
    )
    assert (await _rows(intent))[0].state == "accepted"


@pytest.mark.asyncio
async def test_cancel_runs_through_the_real_store(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    broker = FakeBroker()
    gateway = _gateway(store, broker)
    await gateway.handle(_submit(intent, "child-0001"), confirm=True)
    outcome = await gateway.handle(
        CancelChild(
            action="cancel",
            intent_id=intent.intent_id,
            idempotency_key="child-0002",
            target_idempotency_key="child-0001",
        ),
        confirm=True,
    )
    assert outcome.status == "accepted"
    assert broker.calls[-1] == ("cancel", "1001")


@pytest.mark.asyncio
async def test_db_refuses_a_non_mock_intent_row() -> None:
    intent = _intent()
    async with AsyncSessionLocal() as session:
        session.add(
            ExecutionIntent(
                id=intent.intent_id,
                schema_version=1,
                strategy="s3-test",
                account="kis_live",
                venue="krx",
                symbol="005930",
                side="buy",
                max_quantity=1,
                max_notional_krw=1,
                limit_price_krw=1,
                max_slippage_bps=0,
                position_at_authorization=0,
                min_position_after=0,
                max_position_after=1,
                policy_version="p",
                authorization_ref="a",
                expires_at=_now(),
                intent_digest="d",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_db_refuses_a_finished_state_without_finished_at(
    store: SqlAlchemyGatewayStore,
) -> None:
    intent = _intent()
    await store.store_intent(intent, now=_now())
    await _gateway(store, FakeBroker()).handle(
        _submit(intent, "child-0001"), confirm=True
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(ExecutionChildRequest)
                .where(ExecutionChildRequest.intent_id == intent.intent_id)
                .values(finished_at=None)
            )
        await session.rollback()

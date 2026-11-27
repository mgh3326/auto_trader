"""hk 6991 — the v3 binding against the run-owned test database (runs in CI).

Proves what the memory test cannot: the SQL plan guard reads the daily_plan
tables under the row lock, the v3 CHECKs hold, and the journal trigger accepts
``not_sent`` for a v3 intent only with a v3 reason.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import AsyncSessionLocal, engine
from app.schemas.execution_intent import ExecutionIntentV1, SubmitChild
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.plan_binding import SqlPlanGuard
from app.services.execution_intent_gateway.plan_issuer import (
    IssueRequest,
    build_intent,
)
from app.services.execution_intent_gateway.store import SqlAlchemyGatewayStore
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    FakeBroker,
    FakeKillSwitch,
    journal_tables_lock,
)
from tests.services.execution_intent_gateway.test_plan_version import KEY, SYMBOL, _row

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _cleanup() -> Any:
    async with journal_tables_lock():
        yield
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "TRUNCATE review.execution_child_requests, review.execution_intents"
                )
            )
            await session.commit()


def _now() -> datetime:
    return datetime.now(UTC)


async def _publish(head_id: Any, seq: int, body: dict, kind: str = "event") -> None:
    async with AsyncSessionLocal() as session:
        version_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO review.daily_plan_versions (id, head_id, seq, kind, "
                "idempotency_key, request_sha256, strategy_id, strategy_rule_version, "
                "policy_version, policy_content_hash, metadata) VALUES (:id, :head, "
                ":seq, :kind, :key, 'h', 'plan-entry-test', 'v', 'policy-v1', 'c', "
                "'{}')"
            ),
            {
                "id": version_id,
                "head": head_id,
                "seq": seq,
                "kind": kind,
                "key": f"k{seq}-{uuid4()}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO review.daily_plan_rows (id, version_id, row_kind, market, "
                "symbol, action, action_basis, execution_state, body) VALUES (:id, "
                ":version, 'candidate', 'kr', :symbol, 'new', 'scored', 'executable', "
                "CAST(:body AS jsonb))"
            ),
            {
                "id": uuid4(),
                "version": version_id,
                "symbol": SYMBOL,
                "body": json.dumps(body),
            },
        )
        await session.commit()


async def _head() -> Any:
    head_id = uuid4()
    day = date(2032, 1, 1) + timedelta(days=random.randrange(3000))
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO review.daily_plan_heads (id, market, session_date, mode, "
                "cutoff_at) VALUES (:id, 'kr', :day, 'executable', now())"
            ),
            {"id": head_id, "day": day},
        )
        await session.commit()
    return head_id


def _v3(head_id: Any, seq: int) -> Any:
    body = _row(valid_until=(_now() + timedelta(hours=2)).isoformat())
    return build_intent(
        version={
            "head_id": head_id,
            "seq": seq,
            "strategy_id": "plan-entry-test",
            "policy_version": "policy-v1",
        },
        row={"body": body, "action": "new", "symbol": SYMBOL},
        request=IssueRequest(
            market="kr", day=_now().date(), seq=seq, row_key=KEY, position=0
        ),
        now=_now(),
    )


async def test_the_sql_guard_refuses_a_stale_row_and_allows_a_current_one() -> None:
    head_id = await _head()
    await _publish(head_id, 1, _row(), kind="premarket")
    guard = SqlPlanGuard(AsyncSessionLocal)
    store = SqlAlchemyGatewayStore(AsyncSessionLocal, plan_guard=guard)
    broker = FakeBroker()
    gateway = IntentGateway(
        store=store,
        forwarders={"kis_mock": broker},
        kill_switch=FakeKillSwitch(),
        environment=ENABLED,
        plan_guard=guard,
    )
    intent = _v3(head_id, 1)
    await store.store_intent(intent, now=_now())

    def submit(key: str) -> SubmitChild:
        return SubmitChild(
            action="submit",
            intent_id=intent.intent_id,
            idempotency_key=key,
            quantity=1,
            limit_price_krw=70_000,
        )

    await _publish(head_id, 2, _row(), kind="fill")  # observation only
    first = await gateway.handle(submit("v3-db-child-0001"), confirm=True)
    assert first.status == "accepted" and len(broker.calls) == 1

    await _publish(head_id, 3, _row(would_be_quantity="8"))
    stale = await gateway.handle(submit("v3-db-child-0002"), confirm=True)
    assert (stale.status, stale.reason) == ("refused", "plan_version_stale")
    assert len(broker.calls) == 1


async def _child(intent_id: Any, key: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO review.execution_child_requests (intent_id, "
                "idempotency_key, request_digest, action, quantity, limit_price_krw, "
                "state) VALUES (:intent, :key, 'd', 'submit', 1, 70000, 'sending')"
            ),
            {"intent": intent_id, "key": key},
        )
        await session.commit()


async def _not_sent(key: str, reason: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.execution_child_requests SET state = 'not_sent', "
                "refusal_reason = :reason, finished_at = now() "
                "WHERE idempotency_key = :key"
            ),
            {"key": key, "reason": reason},
        )
        await session.commit()


async def test_the_trigger_takes_not_sent_on_v3_only_with_a_v3_reason() -> None:
    head_id = await _head()
    store = SqlAlchemyGatewayStore(AsyncSessionLocal)
    v3 = _v3(head_id, 1)
    await store.store_intent(v3, now=_now())
    await _child(v3.intent_id, "v3-trigger-0001")
    await _not_sent("v3-trigger-0001", "plan_version_stale")

    await _child(v3.intent_id, "v3-trigger-0002")
    with pytest.raises(DBAPIError, match="not_sent needs a proven reason"):
        await _not_sent("v3-trigger-0002", "submissions_killed")

    v1 = ExecutionIntentV1.model_validate(
        {
            **v3.model_dump(mode="json", exclude={"plan"}),
            "schema_version": 1,
            "intent_id": str(uuid4()),
        }
    )
    await store.store_intent(v1, now=_now())
    await _child(v1.intent_id, "v1-trigger-0001")
    with pytest.raises(DBAPIError, match="not_sent needs a proven reason"):
        await _not_sent("v1-trigger-0001", "plan_version_stale")


async def test_a_v3_row_needs_every_plan_column() -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(DBAPIError, match="ck_execution_intents_v3_shape"):
            await session.execute(
                text(
                    "INSERT INTO review.execution_intents (id, schema_version, strategy, "
                    "account, venue, symbol, side, max_quantity, max_notional_krw, "
                    "limit_price_krw, max_slippage_bps, position_at_authorization, "
                    "min_position_after, max_position_after, policy_version, "
                    "authorization_ref, expires_at, intent_digest) VALUES (:id, 3, 's', "
                    "'kis_mock', 'krx', '005930', 'buy', 1, 70000, 70000, 0, 0, 0, 1, "
                    "'p', 'a', now() + interval '1 hour', 'd')"
                ),
                {"id": uuid4()},
            )
        await session.rollback()

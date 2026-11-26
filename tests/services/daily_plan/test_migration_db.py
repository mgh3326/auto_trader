"""CI-only checks against the real daily-plan migration and service."""

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services.daily_plan import store
from app.services.trading_policy_service import load_trading_policy, policy_content_hash
from tests.services.daily_plan.test_publish import DAY, NOW, request

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TABLES = (
    "daily_plan_heads",
    "daily_plan_input_bundles",
    "daily_plan_versions",
    "daily_plan_rows",
    "daily_plan_outcomes",
    "daily_plan_row_links",
)
_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic/versions/20261126_hk6751_daily_plan.py"
)
_UNRELATED = (
    "order_proposals",
    "order_proposal_rungs",
    "order_send_intents",
    "investment_watch_alerts",
    "investment_watch_events",
    "mandate_plans",
    "mandate_plan_tranches",
    "mandate_plan_events",
)


def _migrate(conn):
    spec = importlib.util.spec_from_file_location("hk6751_migration", _MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # Bootstrap uses metadata.create_all. Run the actual DDL inside the
    # test transaction so its triggers, constraints and ACLs are exercised.
    for table in reversed(_TABLES):
        conn.execute(text(f"DROP TABLE review.{table}"))
    with Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()


async def test_migration_rejects_all_mutations(db_session):
    async with db_session.begin():
        conn = await db_session.connection()
        await conn.run_sync(_migrate)
        for table in _TABLES:
            for sql in (
                f"UPDATE review.{table} SET id = id WHERE false",
                f"DELETE FROM review.{table} WHERE false",
                f"TRUNCATE review.{table}",
            ):
                with pytest.raises(
                    DBAPIError, match="daily plan records are append-only"
                ):
                    async with db_session.begin_nested():
                        await db_session.execute(text(sql))
        await db_session.rollback()


async def test_replay_publish_fold_and_queue_have_no_side_effects(db_session):
    async with db_session.begin():
        before = {
            name: await db_session.scalar(text(f"SELECT count(*) FROM review.{name}"))
            for name in _UNRELATED
        }
        policy = load_trading_policy()
        body = request(idempotency_key=str(uuid4()))
        first = await store.write_version(
            db_session,
            day=DAY,
            request=body,
            now=NOW,
            policy=policy,
            policy_hash=policy_content_hash(),
        )
        retry = await store.write_version(
            db_session,
            day=DAY,
            request=body,
            now=NOW,
            policy=policy,
            policy_hash=policy_content_hash(),
        )
        assert retry["id"] == first["id"]
        second_body = request(
            idempotency_key=str(uuid4()),
            expected_prev_seq=1,
            rows=[{**body["rows"][0], "symbol": "005930", "carry_allowed": False}],
        )
        await store.write_version(
            db_session,
            day=DAY,
            request=second_body,
            now=NOW,
            policy=policy,
            policy_hash=policy_content_hash(),
        )
        expires = datetime(2026, 11, 26, 7, tzinfo=UTC)
        plan = await store.get_plan(
            db_session, day=DAY, seq=None, now=expires, max_carries=5
        )
        assert plan and plan["seq"] == 2
        projected = {item["symbol"]: item for item in plan["rows"]}
        assert projected["278470"]["watch_state"]["carry_count"] == 1
        assert projected["005930"]["watch_state"]["reason"] == "expired"
        read = await store.list_plans(
            db_session,
            now=expires,
            max_carries=5,
            queue_for=date(2026, 11, 27),
        )
        assert [item["symbol"] for item in read["premarket_reevaluation_queue"]] == [
            "278470"
        ]
        await store.confirm_links(db_session, day=DAY, seq=2, now=expires)
        assert {
            name: await db_session.scalar(text(f"SELECT count(*) FROM review.{name}"))
            for name in _UNRELATED
        } == before
        await db_session.rollback()

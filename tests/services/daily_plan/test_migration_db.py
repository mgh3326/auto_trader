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
_SYMBOL_MIGRATION = _MIGRATION.with_name("20261127_hk6769_daily_plan_symbol.py")
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


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _migrate(conn):
    migration = _load(_MIGRATION)
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
                f"TRUNCATE review.{table} CASCADE",
            ):
                with pytest.raises(
                    DBAPIError, match="daily plan records are append-only"
                ):
                    async with db_session.begin_nested():
                        await db_session.execute(text(sql))
        await db_session.rollback()


def _run_symbol_migration(conn, direction):
    with Operations.context(MigrationContext.configure(conn)):
        getattr(_load(_SYMBOL_MIGRATION), direction)()


async def _insert_row(db_session, version_id, symbol):
    async with db_session.begin_nested():
        await db_session.execute(
            text(
                "INSERT INTO review.daily_plan_rows "
                "(id, version_id, row_kind, market, symbol, action, action_basis, body)"
                " VALUES (:id, :version_id, 'candidate', 'kr', :symbol, 'watch', "
                "'scored', '{}')"
            ),
            {"id": uuid4(), "version_id": version_id, "symbol": symbol},
        )


async def test_symbol_check_accepts_alphanumeric_short_codes(db_session):
    async with db_session.begin():
        conn = await db_session.connection()
        await conn.run_sync(_migrate)
        head_id, version_id = uuid4(), uuid4()
        await db_session.execute(
            text(
                "INSERT INTO review.daily_plan_heads "
                "(id, market, session_date, cutoff_at) "
                "VALUES (:id, 'kr', :day, now())"
            ),
            {"id": head_id, "day": date(2031, 1, 6)},
        )
        await db_session.execute(
            text(
                "INSERT INTO review.daily_plan_versions (id, head_id, seq, kind, "
                "idempotency_key, request_sha256, strategy_id, "
                "strategy_rule_version, policy_version, policy_content_hash, "
                "metadata) VALUES (:id, :head, 1, 'session', 'k', 'h', 's', 'v', "
                "'p', 'c', '{}')"
            ),
            {"id": version_id, "head": head_id},
        )
        with pytest.raises(DBAPIError, match="ck_daily_plan_rows_kr_symbol"):
            await _insert_row(db_session, version_id, "0256C0")

        await conn.run_sync(_run_symbol_migration, "upgrade")
        await _insert_row(db_session, version_id, "005930")
        # With digits only, the downgrade restores the old rule and back.
        await conn.run_sync(_run_symbol_migration, "downgrade")
        await conn.run_sync(_run_symbol_migration, "upgrade")
        await _insert_row(db_session, version_id, "0256C0")
        for symbol in ("0256c0", "25C0A", "0256C01", "0256-0"):
            with pytest.raises(DBAPIError, match="ck_daily_plan_rows_kr_symbol"):
                await _insert_row(db_session, version_id, symbol)
        with pytest.raises(RuntimeError, match="alphanumeric short codes"):
            await conn.run_sync(_run_symbol_migration, "downgrade")
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
        stored = await db_session.scalar(
            text(
                "SELECT metadata -> 'allocation_comparison' ->> 'schema_version' "
                "FROM review.daily_plan_versions WHERE id = :id"
            ),
            {"id": first["id"]},
        )
        assert stored == "daily-plan-allocation/v1"
        for response in (first, retry):
            assert "allocation_comparison" not in response["metadata"]
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
        assert "allocation_comparison" not in plan["metadata"]
        # The second version compares the row it carried over as well as its own.
        compared = await db_session.scalar(
            text(
                "SELECT jsonb_array_length("
                "metadata -> 'allocation_comparison' -> 'candidates') "
                "FROM review.daily_plan_versions WHERE head_id = :head AND seq = 2"
            ),
            {"head": plan["head_id"]},
        )
        assert compared == 2
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

"""Exercise migration constraints in the CI-owned PostgreSQL database."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _exercise(connection):
    spec = importlib.util.spec_from_file_location(
        "migration_4182_db",
        Path("alembic/versions/20261112_hk4182_decision_evidence.py"),
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        decision = "11111111-1111-4111-8111-111111111111"
        attempt = "22222222-2222-4222-8222-222222222222"
        inserts = [
            f"INSERT INTO review.decision_evidence (decision_id, decision_kind, decided_by, market, symbol, evidence_hash) VALUES ('{decision}', 'proposal', 'test', 'equity_us', 'TEST', '{'0' * 64}')",
            f"INSERT INTO review.decision_evidence_rungs (decision_id, rung_index, side) VALUES ('{decision}', 0, 'buy')",
            f"INSERT INTO review.decision_reference_prices (decision_id, kind, price) VALUES ('{decision}', 'quote_at_decision', 1)",
            f"INSERT INTO review.decision_order_links (attempt_id, decision_id, account_scope, send_key) VALUES ('{attempt}', '{decision}', 'kis_live', 'same')",
            f"INSERT INTO review.decision_order_link_outcomes (attempt_id, outcome) VALUES ('{attempt}', 'sent')",
        ]
        for statement in inserts:
            connection.execute(text(statement))
        for table in migration.TABLES:
            for statement in (
                f"UPDATE review.{table} SET id=id",
                f"DELETE FROM review.{table}",
                f"TRUNCATE review.{table}",
            ):
                with pytest.raises(DBAPIError), connection.begin_nested():
                    connection.execute(text(statement))
        for statement in inserts:
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(text(statement))
        # A reused send key is legal for a distinct attempt.
        connection.execute(
            text(
                f"INSERT INTO review.decision_order_links (attempt_id, decision_id, account_scope, send_key) VALUES ('33333333-3333-4333-8333-333333333333', '{decision}', 'kis_live', 'same')"
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            migration.downgrade()


async def test_migration_enforces_immutability_unique_keys_and_nonempty_downgrade(
    db_session,
):
    connection = await db_session.connection()
    try:
        await connection.run_sync(_exercise)
    finally:
        await db_session.rollback()

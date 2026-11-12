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
        # The shared test schema now includes L1 for proposal writers. Rebuild
        # its objects inside this rolled-back transaction to test the migration.
        for table in reversed(migration.TABLES):
            connection.execute(text(f"DROP TABLE IF EXISTS review.{table} CASCADE"))
        connection.execute(
            text("DROP FUNCTION IF EXISTS review.hk4182_append_only_guard() CASCADE")
        )
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
        missing = "44444444-4444-4444-8444-444444444444"
        invalid = [
            f"INSERT INTO review.decision_evidence_rungs (decision_id, rung_index, side) VALUES ('{missing}', 0, 'buy')",
            f"INSERT INTO review.decision_reference_prices (decision_id, kind, price) VALUES ('{missing}', 'limit', 1)",
            f"INSERT INTO review.decision_order_links (attempt_id, decision_id, account_scope, send_key) VALUES ('{missing}', '{missing}', 'kis_live', 'key')",
            f"INSERT INTO review.decision_evidence_rungs (decision_id, rung_index, side) VALUES ('{decision}', -1, 'buy')",
            f"INSERT INTO review.decision_reference_prices (decision_id, rung_index, kind, price) VALUES ('{decision}', -1, 'limit', 1)",
            f"INSERT INTO review.decision_order_links (attempt_id, decision_id, account_scope, send_key, proposal_id, rung_index) VALUES ('{missing}', '{decision}', 'kis_live', 'key', '{missing}', -1)",
        ]
        for statement in invalid:
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


async def test_live_proposal_links_evidence_and_cannot_change_decision_id(db_session):
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import uuid4

    from app.services.order_proposals.service import OrderProposalsService, RungInput

    decision_id = None
    try:
        group = await OrderProposalsService(db_session).create_proposal(
            symbol="TEST",
            market="equity_us",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="test",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("12"), None)],
            now=datetime.now(UTC),
            decision_price_text="12.10",
        )
        decision_id = group.decision_id
        evidence = (
            await db_session.execute(
                text(
                    "SELECT decision_kind, symbol FROM review.decision_evidence WHERE decision_id = :id"
                ),
                {"id": decision_id},
            )
        ).one()
        assert evidence == ("proposal", "TEST")
        for replacement in (None, uuid4()):
            with pytest.raises(DBAPIError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            "UPDATE review.order_proposals SET decision_id = :replacement WHERE id = :id"
                        ),
                        {"replacement": replacement, "id": group.id},
                    )
        assert decision_id is not None
    finally:
        await db_session.rollback()


async def test_candidate_inference_excludes_other_days_and_unadmitted(db_session):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import uuid4

    from app.models.screener_pick_log import ScreenerPickLog
    from app.services.order_proposals.service import OrderProposalsService, RungInput

    now = datetime(2026, 11, 12, 2, tzinfo=UTC)
    admitted_id = uuid4()
    try:
        for call_id, at, admission in [
            (uuid4(), now - timedelta(days=1), "admitted"),
            (admitted_id, now - timedelta(hours=1), "admitted"),
            (uuid4(), now - timedelta(minutes=1), "not_admitted"),
            (uuid4(), now + timedelta(minutes=1), "admitted"),
        ]:
            db_session.add(
                ScreenerPickLog(
                    call_id=call_id,
                    recorded_at=at,
                    recorded_at_kst=at.isoformat(),
                    market="us",
                    source="test",
                    family="test",
                    kind="test",
                    symbol="HK4184TEST",
                    admission=admission,
                    fanout_version="test",
                    fanout_code_sha256="0" * 64,
                    source_params={},
                    decision_price_text="11.25",
                )
            )
        await db_session.flush()
        group = await OrderProposalsService(db_session).create_proposal(
            symbol="HK4184TEST",
            market="equity_us",
            account_mode="kis_live",
            side="buy",
            order_type="limit",
            proposer="test",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("12"), None)],
            now=now,
        )
        row = (
            await db_session.execute(
                text(
                    "SELECT candidate_call_id, candidate_link_basis FROM review.decision_evidence WHERE decision_id = :id"
                ),
                {"id": group.decision_id},
            )
        ).one()
        assert row == (admitted_id, "inference")
        price = (
            await db_session.execute(
                text(
                    "SELECT price FROM review.decision_reference_prices WHERE decision_id = :id AND kind = 'candidate_price'"
                ),
                {"id": group.decision_id},
            )
        ).scalar_one()
        assert price == Decimal("11.25")
    finally:
        await db_session.rollback()

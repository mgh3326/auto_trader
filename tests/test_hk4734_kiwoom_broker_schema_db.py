"""hk 4734 (3797 T-P4) — DB proof for the execution-ledger broker widening.

CI only: these tests use the pytest-owned throwaway database. The migration
round trip runs inside one transaction that is rolled back at the end, so the
shared schema is left as bootstrapped.

Covered: both broker CHECKs admit ``'kiwoom'`` in the bootstrapped schema, the
downgrade restores the old vocabulary, the upgrade widens it again, and a
downgrade refuses while a Kiwoom row exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261115_hk4734_kiwoom_broker.py"

LEDGER_TABLE = "execution_ledger"
RUNS_TABLE = "execution_ledger_reconcile_runs"


def _load_migration():
    spec = importlib.util.spec_from_file_location("hk4734_migration_db", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_step(sync_conn, step: str) -> None:
    migration = _load_migration()
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


async def _broker_check_definitions(conn, table: str) -> list[str]:
    # PostgreSQL renders ``IN (...)`` as ``= ANY (ARRAY[...])``, so match the
    # constraint by name (the ORM/migration canonical spelling contains
    # ``broker``) instead of by the definition text.
    rows = (
        await conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                f"WHERE conrelid = 'review.{table}'::regclass AND contype = 'c' "
                "AND conname LIKE '%broker%'"
            )
        )
    ).scalars()
    return list(rows)


async def test_round_trip_widens_both_checks_and_downgrade_refuses_kiwoom_rows() -> (
    None
):
    async with engine.connect() as conn:
        try:
            # Bootstrapped schema already carries the widened vocabulary.
            assert any(
                "'kiwoom'" in definition
                for definition in await _broker_check_definitions(conn, LEDGER_TABLE)
            )
            assert any(
                "'kiwoom'" in definition
                for definition in await _broker_check_definitions(conn, RUNS_TABLE)
            )

            await conn.run_sync(_run_step, "downgrade")
            for table in (LEDGER_TABLE, RUNS_TABLE):
                definitions = await _broker_check_definitions(conn, table)
                assert definitions
                assert all("kiwoom" not in definition for definition in definitions)

            await conn.run_sync(_run_step, "upgrade")
            for table in (LEDGER_TABLE, RUNS_TABLE):
                assert any(
                    "'kiwoom'" in definition
                    for definition in await _broker_check_definitions(conn, table)
                )

            await conn.execute(
                text(
                    "INSERT INTO review.execution_ledger "
                    "(broker, account_mode, venue, instrument_type, symbol, "
                    "raw_symbol, side, broker_order_id, fill_seq, filled_qty, "
                    "filled_price, filled_notional, filled_at, currency, source) "
                    "VALUES ('kiwoom','mock','krx','equity_kr','005930','005930',"
                    "'buy','93000001',7,1,70000,70000,now(),'KRW','reconciler')"
                )
            )
            with pytest.raises(DBAPIError) as excinfo:
                await conn.run_sync(_run_step, "downgrade")
            assert "restrict_violation" in str(
                excinfo.value
            ) or "export before downgrading" in str(excinfo.value)
        finally:
            await conn.rollback()

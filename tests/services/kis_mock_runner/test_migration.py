"""KR-B0 control-table migration proof, including pytest-owned PostgreSQL DDL."""

from __future__ import annotations

import ast
import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260805_kis_mock_runner_control.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "kis_mock_runner_control", _MIGRATION_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def test_migration_is_additive_and_chains_from_current_head() -> None:
    tree = ast.parse(_MIGRATION_PATH.read_text(encoding="utf-8"))
    assignments = {
        node.target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert assignments["revision"] == "20260805_kis_mock_runner"
    assert assignments["down_revision"] == "20260804_alpaca_clean_account"
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "op.create_table(" in source
    assert "op.drop_table(" in source
    assert "kis_mock_runner_control" in source
    assert "alter_" not in source
    assert "drop_column" not in source


def test_offline_migration_renders_singleton_mode_constraints() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    original_op = _MIGRATION.op
    _MIGRATION.op = Operations(context)
    try:
        _MIGRATION.upgrade()
    finally:
        _MIGRATION.op = original_op
    sql = output.getvalue()
    assert "CREATE TABLE review.kis_mock_runner_control" in sql
    assert "CHECK (id = 1)" in sql
    assert "'ACTIVE','ENTRY_HALT','GLOBAL_FREEZE'" in sql
    assert "initial_control_row" in sql


@pytest.mark.asyncio
async def test_migration_applies_and_round_trips_on_pytest_owned_database(
    db_session,
) -> None:
    """No operator database is touched: db_session selects a run-owned test DB."""
    del db_session
    from app.core.db import engine

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:

            def upgrade(sync_connection) -> None:
                context = MigrationContext.configure(sync_connection)
                original_op = _MIGRATION.op
                _MIGRATION.op = Operations(context)
                try:
                    _MIGRATION.upgrade()
                finally:
                    _MIGRATION.op = original_op

            await connection.run_sync(upgrade)
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, mode, reason, updated_by "
                            "FROM review.kis_mock_runner_control WHERE id = 1"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "id": 1,
                "mode": "ACTIVE",
                "reason": "initial_control_row",
                "updated_by": "migration:KR-B0",
            }
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO review.kis_mock_runner_control "
                        "(id, mode, reason, updated_by) "
                        "VALUES (2, 'ACTIVE', 'bad', 'test')"
                    )
                )
            await transaction.rollback()
        finally:
            if transaction.is_active:
                await transaction.rollback()

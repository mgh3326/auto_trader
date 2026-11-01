"""Migration shape (offline render, no database) and the module's safety boundary."""

from __future__ import annotations

import ast
import importlib.util
import io
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base
from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261101_rob1440_intent_gateway.py"
PACKAGE = ROOT / "app/services/execution_intent_gateway"


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("rob1440_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_upgrade() -> str:
    module = _load_migration()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        # Alembic applies this naming convention at upgrade time, as env.py does.
        opts={
            "as_sql": True,
            "output_buffer": buffer,
            "target_metadata": Base.metadata,
        },
    )
    with Operations.context(context):
        module.upgrade()
    return buffer.getvalue()


def _table_block(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE review.{table} (")
    return sql[start : sql.index(";", start)]


def test_migration_is_additive_and_grants_no_delete() -> None:
    sql = _render_upgrade()
    assert "ALTER TABLE" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    assert "GRANT SELECT, INSERT ON review.execution_intents" in sql
    grants = sql.split("DO $$", 1)[1]
    assert "DELETE" not in grants
    assert "GRANT SELECT, INSERT ON review.execution_child_requests" in grants
    # SELECT ... FOR UPDATE on the intent row needs UPDATE on one column.
    assert "GRANT UPDATE (id) ON review.execution_intents" in grants
    assert (
        "GRANT UPDATE (state, broker_order_id, result, finished_at) "
        "ON review.execution_child_requests" in grants
    )


def test_journal_guard_covers_every_column_except_the_outcome_columns() -> None:
    guard = next(
        statement
        for statement in _load_migration().GUARD_DDL
        if "execution_child_requests_guard()" in statement
        and "CREATE OR REPLACE" in statement
    )
    outcome_columns = {"state", "broker_order_id", "result", "finished_at"}
    for column in ExecutionChildRequest.__table__.columns:
        compared = f"NEW.{column.name} IS DISTINCT FROM OLD.{column.name}" in guard
        assert compared is (column.name not in outcome_columns), column.name


def test_guards_refuse_update_and_delete_on_both_tables() -> None:
    sql = _render_upgrade()
    for table, trigger in (
        ("execution_intents", "trg_execution_intents_immutable"),
        ("execution_child_requests", "trg_execution_child_requests_guard"),
    ):
        assert f"CREATE TRIGGER {trigger}" in sql
        assert f"BEFORE UPDATE OR DELETE ON review.{table}" in sql


@pytest.mark.parametrize("model", [ExecutionIntent, ExecutionChildRequest])
def test_migration_columns_and_checks_match_the_model(model: type) -> None:
    table: sa.Table = model.__table__  # type: ignore[attr-defined]
    block = _table_block(_render_upgrade(), table.name)
    for column in table.columns:
        assert f"{column.name} " in block, column.name
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert checks
    for check in checks:
        assert str(check.sqltext) in block, str(check.sqltext)
        assert f"CONSTRAINT {check.name} CHECK" in block, check.name
    assert f"ck_{table.name}_ck_" not in block


def test_only_mock_is_allowed_in_the_account_check() -> None:
    assert "account IN ('kis_mock')" in _table_block(
        _render_upgrade(), "execution_intents"
    )


def test_forwarder_pins_mock_on_every_broker_call() -> None:
    tree = ast.parse((PACKAGE / "kis_mock_forwarder.py").read_text())
    broker_calls = {"_place_order_impl", "modify_order_impl", "cancel_order_impl"}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in broker_calls
        ):
            seen.add(node.func.id)
            pinned = {
                keyword.arg: keyword.value
                for keyword in node.keywords
                if keyword.arg == "is_mock"
            }
            value = pinned["is_mock"]
            assert isinstance(value, ast.Constant) and value.value is True
    assert seen == broker_calls


def test_package_imports_no_broker_client_ledger_or_live_tool_at_module_level() -> None:
    forbidden_prefixes = (
        "app.services.brokers",
        "app.mcp_server",
        "app.services.toss",
        "app.services.kis_live",
        "app.services.execution_ledger",
    )
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                assert not name.startswith(forbidden_prefixes), f"{path.name}: {name}"


def test_nothing_in_the_app_wires_the_gateway_to_a_transport_or_scheduler() -> None:
    importers = []
    for path in (ROOT / "app").rglob("*.py"):
        if PACKAGE in path.parents:
            continue
        if "execution_intent_gateway" in path.read_text(encoding="utf-8"):
            importers.append(path.relative_to(ROOT).as_posix())
    assert importers == []

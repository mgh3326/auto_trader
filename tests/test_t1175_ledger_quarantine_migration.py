"""#1175 — static contract for the execution-ledger quarantine migration.

The live proof (upgrade -> downgrade -> upgrade against a throwaway
TimescaleDB PostgreSQL, schema dump identical after downgrade, and the CHECK /
trigger refusals exercised by hand) is recorded in
docs/runbooks/execution-ledger-quarantine.md. CI builds the test schema with
create_all plus tests/_schema_bootstrap.py, so this file pins that the three
copies (migration, ORM model, test bootstrap) say the same thing.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from app.models import execution_ledger as model
from tests import _schema_bootstrap as bootstrap

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20261001_t1175_ledger_quarantine.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_t1175_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_revision_chain() -> None:
    migration = _load_migration()
    assert migration.revision == "20261001_t1175_ledger_quar"
    assert migration.down_revision == "20260930_rob1120_quotes"
    assert len(migration.revision) <= 32


def test_model_and_migration_checks_are_the_same_sql() -> None:
    migration = _load_migration()
    assert migration.QUARANTINE_FIELDS_SQL == model.QUARANTINE_FIELDS_SQL
    assert migration.QUARANTINE_SCOPE_SQL == model.QUARANTINE_SCOPE_SQL


def test_bootstrap_mirrors_the_migration_triggers_and_checks() -> None:
    migration = _load_migration()
    ddl = [_norm(s) for s in bootstrap._DDL_STATEMENTS]
    assert _norm(migration.GUARD_FUNCTION_DDL) in ddl
    assert _norm(migration.AUDIT_REJECT_FUNCTION_DDL) in ddl
    joined = "\n".join(ddl)
    assert f"CHECK ({_norm(model.QUARANTINE_FIELDS_SQL)})" in joined
    assert f"CHECK ({_norm(model.QUARANTINE_SCOPE_SQL)})" in joined
    for trigger in (
        "trg_execution_ledger_quarantine_guard",
        "trg_execution_ledger_quarantine_events_append_only",
        "trg_execution_ledger_quarantine_events_truncate",
        "trg_execution_ledger_quarantine_truncate",
    ):
        assert f"CREATE TRIGGER {trigger} " in joined
        assert trigger in MIGRATION.read_text("utf-8")


def _op_calls(function: str) -> list[ast.Call]:
    tree = ast.parse(MIGRATION.read_text("utf-8"))
    [node] = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function
    ]
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "op"
    ]


def test_upgrade_only_adds_and_never_rewrites_ledger_data() -> None:
    migration = _load_migration()
    names = [call.func.attr for call in _op_calls("upgrade")]  # type: ignore[attr-defined]
    assert "drop_column" not in names
    assert "alter_column" not in names
    assert "drop_table" not in names
    assert names.count("add_column") == 3
    for call in _op_calls("upgrade"):
        if call.func.attr == "add_column":  # type: ignore[attr-defined]
            assert ast.literal_eval(call.args[0]) == "execution_ledger"
            [column] = [a for a in call.args if isinstance(a, ast.Call)]
            nullable = {kw.arg: kw.value for kw in column.keywords}["nullable"]
            assert isinstance(nullable, ast.Constant) and nullable.value is True
    executed = " ".join(
        [
            migration.GUARD_FUNCTION_DDL,
            migration.AUDIT_REJECT_FUNCTION_DDL,
            MIGRATION.read_text("utf-8"),
        ]
    ).upper()
    assert "UPDATE REVIEW.EXECUTION_LEDGER" not in executed
    assert "DELETE FROM" not in executed
    assert "TRUNCATE REVIEW" not in executed


def test_downgrade_reverses_everything_upgrade_creates() -> None:
    text = MIGRATION.read_text("utf-8")
    down = text[text.index("def downgrade") :]
    for name in (
        "trg_execution_ledger_quarantine_truncate",
        "trg_execution_ledger_quarantine_events_truncate",
        "trg_execution_ledger_quarantine_events_append_only",
        "reject_execution_ledger_quarantine_event_mutation",
        "ix_execution_ledger_quarantine_events_batch",
        "execution_ledger_quarantine_events",
        "trg_execution_ledger_quarantine_guard",
        "guard_execution_ledger_quarantine",
        "ck_execution_ledger_quarantine_scope",
        "ck_execution_ledger_quarantine_fields",
        "quarantined_by",
        "quarantine_reason",
        "quarantined_at",
    ):
        assert name in down, name


def test_orm_constraint_names_match_the_migration() -> None:
    ledger = {c.name for c in model.ExecutionLedger.__table__.constraints}
    assert {
        "ck_execution_ledger_quarantine_fields",
        "ck_execution_ledger_quarantine_scope",
    } <= ledger
    events = {
        c.name for c in model.ExecutionLedgerQuarantineEvent.__table__.constraints
    }
    text = MIGRATION.read_text("utf-8")
    for name in events:
        assert f'"{name}"' in text, name


def test_quarantined_rows_are_terminal_in_every_copy() -> None:
    migration = _load_migration()
    guard = _norm(migration.GUARD_FUNCTION_DDL)
    assert "IF OLD.quarantined_at IS NOT NULL THEN RAISE EXCEPTION" in guard
    assert "ERRCODE = 'restrict_violation'" in guard
    assert "IF TG_OP = 'TRUNCATE' THEN" in guard
    text = MIGRATION.read_text("utf-8")
    migration_sql = "\n".join(
        _norm(node.value)
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    joined = "\n".join(_norm(s) for s in bootstrap._DDL_STATEMENTS)
    for source in (migration_sql, joined):
        assert (
            "CREATE TRIGGER trg_execution_ledger_quarantine_guard "
            "BEFORE UPDATE OR DELETE ON review.execution_ledger" in source
        )
        assert (
            "CREATE TRIGGER trg_execution_ledger_quarantine_truncate "
            "BEFORE TRUNCATE ON review.execution_ledger" in source
        )
    # round 4 removed the replay tombstone everywhere it existed
    for name in ("requarantine", "is_tombstoned", "tombstone"):
        assert name not in text.lower()
    repo = (
        MIGRATION.parents[2] / "app/services/execution_ledger/repository.py"
    ).read_text("utf-8")
    assert "tombstone" not in repo.lower()
    assert not (
        MIGRATION.parents[2] / "app/services/execution_ledger/accept_notice.py"
    ).exists()

"""Tests for the changed-migration expand-contract CI lint."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci import migration_expand_contract_lint as lint


@pytest.mark.parametrize(
    "operation",
    [
        "op.drop_table('old_table')",
        "op.drop_column('items', 'old_column')",
        "op.rename_table('old_table', 'new_table')",
        "op.alter_column('items', 'old_name', new_column_name='new_name')",
        "op.alter_column('items', 'value', nullable=False)",
        "op.alter_column('items', 'value', False)",
        "op.alter_column('items', 'value', type_=sa.BigInteger())",
        "op.execute('DROP TABLE old_table')",
        "op.execute('ALTER TABLE items DROP COLUMN old_column')",
        "op.execute('ALTER TABLE items DROP old_column')",
        "op.execute('ALTER TABLE items RENAME COLUMN old TO new')",
        "op.execute('ALTER TABLE items ALTER COLUMN value SET NOT NULL')",
        "op.execute('ALTER TABLE items ALTER COLUMN value TYPE BIGINT')",
        "op.execute('ALTER TABLE items ALTER COLUMN value SET DATA TYPE BIGINT')",
        "op.execute('ALTER TABLE items ALTER COLUMN value NOT NULL')",
    ],
)
def test_forbidden_upgrade_operations_require_marker(operation: str) -> None:
    source = f"def upgrade():\n    {operation}\n\ndef downgrade():\n    pass\n"
    marked = (
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {operation}\n"
    )
    assert lint.violations(source, "4200")
    assert lint.violations(marked, "4200") == []


def test_additive_table_and_nullable_column_pass() -> None:
    source = """\
def upgrade():
    op.create_table('new_table', sa.Column('id', sa.Integer()))
    op.add_column('items', sa.Column('extra', sa.String(), nullable=True))

def downgrade():
    op.drop_column('items', 'extra')
    op.drop_table('new_table')
"""
    assert lint.violations(source, "4200") == []


def test_batch_operations_and_reachable_module_helper_are_checked() -> None:
    source = """\
def upgrade():
    with op.batch_alter_table('items') as batch_op:
        batch_op.drop_column('old_column')
    remove_old_table()

def remove_old_table():
    op.drop_table('old_table')
"""
    problems = lint.violations(source, "4200")
    assert len(problems) == 2

    marked = """\
def upgrade():
    with op.batch_alter_table('items') as batch_op:
        # expand-contract: contract-step 4200
        batch_op.drop_column('old_column')
    remove_old_table()

def remove_old_table():
    # expand-contract: contract-step 4200
    op.drop_table('old_table')
"""
    assert lint.violations(marked, "4200") == []


def test_marker_only_covers_the_immediately_following_operation() -> None:
    source = """\
def upgrade():
    # expand-contract: contract-step 4200
    op.drop_table('old_table')
    op.drop_column('items', 'old_column')
"""
    assert len(lint.violations(source, "4200")) == 1


def test_marker_task_id_must_match_pr_task_id() -> None:
    source = """\
def upgrade():
    # expand-contract: contract-step 9999
    op.drop_table('old_table')
"""
    assert lint.violations(source, "4200")


def test_raw_sql_constants_and_concatenation_are_checked() -> None:
    source = """\
DDL_PREFIX = 'DROP ' + 'TABLE '
DDL = DDL_PREFIX + 'old_table'

def upgrade():
    op.execute(DDL)
"""
    assert lint.violations(source, "4200")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX old_index",
        "ALTER TABLE items DROP CONSTRAINT old_constraint",
    ],
)
def test_drop_index_and_constraint_are_outside_column_drop_rule(sql: str) -> None:
    source = f"def upgrade():\n    op.execute({sql!r})\n"
    assert lint.violations(source, "4200") == []


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE items ALTER COLUMN value DROP NOT NULL",
        "ALTER TABLE items ALTER COLUMN value DROP DEFAULT",
        "ALTER TABLE items ALTER COLUMN value DROP IDENTITY",
    ],
)
def test_relaxing_column_constraints_is_not_a_contract_operation(sql: str) -> None:
    source = f"def upgrade():\n    op.execute({sql!r})\n"
    assert lint.violations(source, "4200") == []


def test_execute_keyword_sqltext_is_checked_and_dynamic_missing_argument_fails_closed() -> (
    None
):
    source = "def upgrade():\n    op.execute(sqltext='DROP TABLE old_table')\n"
    assert lint.violations(source, "4200")

    missing = "def upgrade():\n    op.execute(encoding='utf-8')\n"
    assert lint.violations(missing, "4200")

    dynamic = "def upgrade():\n    op.execute(sqltext=build_ddl())\n"
    assert lint.violations(dynamic, "4200")
    marked = (
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    op.execute(sqltext=build_ddl())\n"
    )
    assert lint.violations(marked, "4200") == []
    multiline = (
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    op.execute(\n        sqltext=build_ddl()\n    )\n"
    )
    assert lint.violations(multiline, "4200") == []


def test_text_wrapper_requires_a_trusted_sqlalchemy_import() -> None:
    trusted = (
        "from sqlalchemy import text\n"
        "def upgrade():\n    op.execute(text('DROP TABLE old'))\n"
    )
    assert lint.violations(trusted, "4200")
    untrusted = (
        "from helpers import text\n"
        "def upgrade():\n    op.execute(text('DROP TABLE old'))\n"
    )
    assert lint.violations(untrusted, "4200")


def test_dynamic_raw_sql_fails_closed() -> None:
    source = "def upgrade():\n    op.execute(f'DROP TABLE {table_name}')\n"
    assert any(
        "not a string literal" in item for item in lint.violations(source, "4200")
    )


def test_raw_sql_assigned_inside_upgrade_is_checked() -> None:
    source = "def upgrade():\n    statement = 'DROP TABLE old_table'\n    op.execute(statement)\n"
    assert lint.violations(source, "4200")


def test_reassigned_sql_name_fails_closed_instead_of_reusing_a_literal() -> None:
    source = "def upgrade():\n    sql = 'SELECT 1'\n    sql = build_ddl()\n    op.execute(sql)\n"
    problems = lint.violations(source, "4200")
    assert any("unknown helper 'build_ddl'" in item for item in problems)
    assert any("not a string literal" in item for item in problems)

    marked = (
        "def upgrade():\n    sql = 'SELECT 1'\n"
        "    # expand-contract: contract-step 4200\n    sql = build_ddl()\n"
        "    # expand-contract: contract-step 4200\n    op.execute(sql)\n"
    )
    assert lint.violations(marked, "4200") == []

    augmented = (
        "def upgrade():\n    sql = 'SELECT 1'\n    sql += suffix\n    op.execute(sql)\n"
    )
    assert lint.violations(augmented, "4200")


@pytest.mark.parametrize(
    "body",
    [
        "from helpers import remove_old_table\ndef upgrade():\n    remove_old_table()",
        "def remove_old_table():\n    op.drop_table('old')\ndef upgrade():\n    handler = remove_old_table\n    handler()",
        "def upgrade():\n    unknown_helper()",
        "def upgrade():\n    exec('op.drop_table(\"old\")')",
        "def upgrade():\n    for item in items:\n        op.add_column('items', sa.Column('x', sa.String()))",
        "def upgrade():\n    alias = op\n    alias.drop_table('old')",
        "def upgrade():\n    op = unsafe\n    op.create_table('items')",
        "def upgrade():\n    sa = unsafe\n    sa.Column('id', sa.Integer())",
        "def helper():\n    pass\ndef upgrade():\n    helper = unsafe\n    helper()",
    ],
)
def test_unrecognized_helper_alias_or_dynamic_code_fails_closed(body: str) -> None:
    assert lint.violations(body, "4200")


def test_unknown_helper_can_only_pass_with_local_contract_marker() -> None:
    source = (
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    external_helper()\n"
    )
    assert lint.violations(source, "4200") == []

    imported = (
        "from helpers import external_helper\n"
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    external_helper()\n"
    )
    assert lint.violations(imported, "4200") == []


@pytest.mark.parametrize(
    "expression",
    [
        "connection.exec_driver_sql(sql)",
        "connection.execute(sql)",
        "op.execute(sql)",
    ],
)
def test_exec_driver_sql_and_connection_execute_use_literal_sql_only(
    expression: str,
) -> None:
    source = f"def upgrade():\n    {expression}\n"
    assert any("string literal" in item for item in lint.violations(source, "4200"))
    marked = (
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {expression}\n"
    )
    assert lint.violations(marked, "4200") == []


def test_exec_driver_sql_literal_destructive_sql_requires_marker() -> None:
    source = "def upgrade():\n    conn.exec_driver_sql('ALTER TABLE items DROP COLUMN old')\n"
    assert lint.violations(source, "4200")
    marked = (
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    conn.exec_driver_sql('ALTER TABLE items DROP COLUMN old')\n"
    )
    assert lint.violations(marked, "4200") == []


@pytest.mark.parametrize(
    "operation",
    [
        "op.alter_column('items', 'value', nullable=NOT_NULL)",
        "op.alter_column('items', 'value', nullable=maybe_nullable)",
        "op.alter_column('items', 'value', nullable=1)",
        "op.alter_column('items', 'value', **kwargs)",
        "op.alter_column('items', 'old', None, None, None, 'new')",
        "op.alter_column('items', 'value', None, None, None, None, sa.BigInteger())",
    ],
)
def test_dynamic_and_positional_alter_column_forms_fail_closed(
    operation: str,
) -> None:
    source = f"def upgrade():\n    {operation}\n"
    assert lint.violations(source, "4200")
    marked = (
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {operation}\n"
    )
    assert lint.violations(marked, "4200") == []


def test_only_known_expand_calls_and_literal_sql_are_green() -> None:
    source = """\
def upgrade():
    op.create_table('items', sa.Column('id', sa.Integer()))
    op.add_column('items', sa.Column('extra', sa.String(), nullable=True))
    op.create_index('ix_items_extra', 'items', ['extra'])
    op.create_foreign_key('fk_items', 'items', 'other', ['other_id'], ['id'])
    op.alter_column('items', 'extra', nullable=True)
    op.execute('ALTER TABLE items ADD COLUMN another TEXT')
"""
    assert lint.violations(source, "4200") == []


def test_loop_operation_is_allowed_only_with_operation_marker() -> None:
    source = (
        "def upgrade():\n    for table in tables:\n"
        "        op.create_table(table, sa.Column('id', sa.Integer()))\n"
    )
    assert lint.violations(source, "4200")
    marked = (
        "def upgrade():\n    for table in tables:\n"
        "        # expand-contract: contract-step 4200\n"
        "        op.create_table(table, sa.Column('id', sa.Integer()))\n"
    )
    assert lint.violations(marked, "4200") == []

    helper_loop = (
        "def helper():\n    op.create_table('items')\n"
        "def upgrade():\n    for table in tables:\n        helper()\n"
    )
    assert any(
        "helper call inside a loop" in item
        for item in lint.violations(helper_loop, "4200")
    )


def test_forbidden_operation_in_downgrade_does_not_fail() -> None:
    source = "def upgrade():\n    pass\n\ndef downgrade():\n    op.drop_table('old')\n"
    assert lint.violations(source, "4200") == []


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Migration Lint Test",
        "GIT_AUTHOR_EMAIL": "migration-lint@example.invalid",
        "GIT_COMMITTER_NAME": "Migration Lint Test",
        "GIT_COMMITTER_EMAIL": "migration-lint@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_only_changed_migrations_are_checked(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Migration Lint Test")
    _git(tmp_path, "config", "user.email", "migration-lint@example.invalid")
    versions = tmp_path / "alembic/versions"
    versions.mkdir(parents=True)
    historic = versions / "historic.py"
    historic.write_text("def upgrade():\n    op.drop_table('historic')\n")
    base = _commit(tmp_path, "base with historic migration")

    additive = versions / "additive.py"
    additive.write_text(
        "def upgrade():\n    op.add_column('items', sa.Column('extra', sa.String()))\n"
    )
    head = _commit(tmp_path, "add additive migration")
    assert lint.changed_migrations(tmp_path, base, head) == [additive]
    assert lint.check_paths(lint.changed_migrations(tmp_path, base, head), "4200") == []

    forbidden = versions / "changed.py"
    forbidden.write_text("def upgrade():\n    op.drop_table('changed')\n")
    new_head = _commit(tmp_path, "add forbidden migration")
    changed = lint.changed_migrations(tmp_path, head, new_head)
    assert changed == [forbidden]
    assert lint.check_paths(changed, "4200")

"""Tests for the changed-migration expand-contract CI lint."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci import migration_expand_contract_lint as lint


def _violations(source: str, task_id: str = "4200") -> list[str]:
    """Add canonical Alembic boilerplate to operation-focused unit snippets."""
    tree = ast.parse(source)
    imported_names: set[str] = set()
    metadata_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported_names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Assign):
            metadata_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    header: list[str] = []
    has_docstring = bool(
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    )
    if not has_docstring:
        header.append('"""Test migration."""')
    if "op" not in imported_names:
        header.append("from alembic import op")
    if "sa" not in imported_names:
        header.append("import sqlalchemy as sa")
    if "postgresql" not in imported_names:
        header.append("from sqlalchemy.dialects import postgresql")
    for name, value in (
        ("revision", "'test_revision'"),
        ("down_revision", "None"),
        ("branch_labels", "None"),
        ("depends_on", "None"),
    ):
        if name not in metadata_names:
            header.append(f"{name} = {value}")

    lines = source.splitlines()
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        insert_at = tree.body[0].end_lineno or 1
    elif lines and lint.CONTRACT_MARKER_RE.fullmatch(lines[0]):
        insert_at = 1
    else:
        insert_at = 0
    if header:
        lines[insert_at:insert_at] = [*header, ""]

    top_level_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "upgrade" in top_level_names and "downgrade" not in top_level_names:
        lines.extend(["", "def downgrade():", "    pass"])
    return lint.violations("\n".join(lines) + "\n", task_id)


def _file_marker(source: str) -> str:
    return f"# expand-contract: contract-step 4200\n{source}"


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
        "op.execute('DROP/*x*/TABLE old_table')",
        "op.execute('ALTER TABLE items DROP COLUMN old_column')",
        "op.execute('ALTER/*x*/TABLE items DROP/*x*/COLUMN old_column')",
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
    assert _violations(source, "4200")
    assert _violations(marked, "4200") == []


@pytest.mark.parametrize(
    "source",
    [
        "def _up():\n    op.drop_table('old')\nupgrade = _up\n",
        "upgrade = lambda: op.drop_table('old')\n",
        "if enabled:\n    def upgrade():\n        op.drop_table('old')\n",
    ],
)
def test_upgrade_must_be_a_single_top_level_function(source: str) -> None:
    assert _violations(source, "4200")


def test_module_level_migration_operation_cannot_escape_the_lint() -> None:
    source = "op.drop_table('old')\ndef upgrade():\n    pass\n"
    assert _violations(source, "4200")
    marked = (
        "# expand-contract: contract-step 4200\n"
        "op.drop_table('old')\ndef upgrade():\n    pass\n"
    )
    assert _violations(marked, "4200") == []


def test_module_helper_reference_requires_a_file_marker() -> None:
    source = """\
def _drop(table):
    op.drop_table(table)

def upgrade():
    max(["old"], key=_drop)
"""
    assert any(
        "module shape is indeterminate" in item for item in _violations(source, "4200")
    )
    assert _violations(_file_marker(source), "4200") == []


@pytest.mark.parametrize(
    "source",
    [
        "class _Cleanup:\n    op.drop_table('old')\ndef upgrade():\n    pass\n",
        "def _drop_old(table):\n    op.drop_table(table)\n    return lambda fn: fn\n\n@_drop_old('old')\ndef upgrade():\n    pass\n",
        "def _drop_old(table):\n    op.drop_table(table)\n    return table\n\n@upgrade_decorator\ndef upgrade(value=_drop_old('old')):\n    pass\n",
    ],
)
def test_import_time_class_and_function_expressions_are_checked(source: str) -> None:
    assert any(
        "module shape is indeterminate" in item for item in _violations(source, "4200")
    )
    assert _violations(_file_marker(source), "4200") == []


def test_indeterminate_module_requires_a_file_header_marker() -> None:
    source = (
        "class _Cleanup:\n"
        "    # expand-contract: contract-step 4200\n"
        "    op.drop_table('old')\n"
        "def upgrade():\n    pass\n"
    )
    assert _violations(source, "4200")
    marked = "# expand-contract: contract-step 4200\n" + source
    assert _violations(marked, "4200") == []
    wrong_task = "# expand-contract: contract-step 9999\n" + source
    assert _violations(wrong_task, "4200")


def test_closed_module_shape_allows_only_literal_metadata_and_two_plain_functions() -> (
    None
):
    valid = '''\
"""Test migration."""
from alembic import op
import sqlalchemy as sa
revision = "test_revision"
down_revision = None
branch_labels = None
depends_on = None
def upgrade():
    op.create_table("items", sa.Column("id", sa.Integer()))
def downgrade():
    op.drop_table("items")
'''
    assert lint.violations(valid, "4200") == []

    missing_downgrade = valid.replace(
        'def downgrade():\n    op.drop_table("items")\n', ""
    )
    assert any(
        "exactly upgrade() and downgrade()" in item
        for item in lint.violations(missing_downgrade, "4200")
    )

    decorated = valid.replace("def upgrade():", "@cleanup\ndef upgrade():")
    assert any(
        "module shape is indeterminate" in item
        for item in lint.violations(decorated, "4200")
    )


def test_standard_annotated_create_table_and_add_column_need_no_marker() -> None:
    source = '''\
"""Standard typed Alembic migration."""
from __future__ import annotations
from typing import Sequence, Union
from collections.abc import Iterable
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision: str = "standard"
down_revision: Union[str, None] = "parent"
branch_labels: Sequence[str] | None = None
depends_on: Iterable[str] | None = None
def upgrade() -> None:
    """Add a table and an optional column."""
    op.create_table(
        "cache",
        sa.Column("nid", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("nid", name=op.f("pk_cache")),
        sa.Column("fetched_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("items", sa.Column("extra", sa.Text(), nullable=True))
def downgrade() -> None:
    """Reverse this migration."""
    pass
'''
    assert lint.violations(source, "4200") == []


def test_sql_comment_tokens_cannot_hide_ddl_but_quoted_text_is_preserved() -> None:
    assert (
        "destructive SQL"
        in _violations("def upgrade():\n    op.execute('DROP/*x*/TABLE old_table')\n")[
            0
        ]
    )
    assert (
        "destructive SQL"
        in _violations(
            "def upgrade():\n    op.execute('ALTER/*x*/TABLE items DROP/*x*/COLUMN old')\n"
        )[0]
    )
    assert (
        _violations(
            """def upgrade():
    op.execute("SELECT 'DROP/*x*/TABLE' AS note")
"""
        )
        == []
    )


def test_existing_rob811_standard_migration_is_analyzable() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/rob811_naver_research_detail_cache.py"
    )
    assert lint.violations(path.read_text(), "4200") == []


def test_rebound_module_helper_name_requires_a_file_marker() -> None:
    source = """\
def helper():
    pass

def _drop():
    op.drop_table("old")

helper = _drop

def upgrade():
    helper()
"""
    assert any(
        "module shape is indeterminate" in item for item in _violations(source, "4200")
    )
    assert _violations(_file_marker(source), "4200") == []


@pytest.mark.parametrize(
    "operation",
    [
        "op.alter_column(*params)",
        "op.alter_column('items', *rest)",
        "op.alter_column('items', 'value', *nullable_and_rest)",
    ],
)
def test_starred_alter_column_arguments_fail_closed(operation: str) -> None:
    source = f"def upgrade():\n    {operation}\n"
    assert _violations(source, "4200")
    marked = _file_marker(
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {operation}\n"
    )
    assert _violations(marked, "4200") == []


def test_additive_table_and_nullable_column_pass() -> None:
    source = """\
def upgrade():
    op.create_table('new_table', sa.Column('id', sa.Integer()))
    op.add_column('items', sa.Column('extra', sa.String(), nullable=True))

def downgrade():
    op.drop_column('items', 'extra')
    op.drop_table('new_table')
"""
    assert _violations(source, "4200") == []


def test_batch_operations_and_reachable_module_helper_are_checked() -> None:
    source = """\
def upgrade():
    with op.batch_alter_table('items') as batch_op:
        batch_op.drop_column('old_column')
    remove_old_table()

def remove_old_table():
    op.drop_table('old_table')
"""
    problems = _violations(source, "4200")
    assert len(problems) == 1
    assert "module shape is indeterminate" in problems[0]

    marked = """\
# expand-contract: contract-step 4200
def upgrade():
    with op.batch_alter_table('items') as batch_op:
        # expand-contract: contract-step 4200
        batch_op.drop_column('old_column')
    remove_old_table()

def remove_old_table():
    # expand-contract: contract-step 4200
    op.drop_table('old_table')
"""
    assert _violations(marked, "4200") == []

    rebound = """\
def upgrade():
    with op.batch_alter_table('items') as batch:
        pass
    batch = unsafe
    batch.add_column(sa.Column('extra', sa.String()))
"""
    assert _violations(rebound, "4200")


def test_marker_only_covers_the_immediately_following_operation() -> None:
    source = """\
def upgrade():
    # expand-contract: contract-step 4200
    op.drop_table('old_table')
    op.drop_column('items', 'old_column')
"""
    assert len(_violations(source, "4200")) == 1


def test_marker_task_id_must_match_pr_task_id() -> None:
    source = """\
def upgrade():
    # expand-contract: contract-step 9999
    op.drop_table('old_table')
"""
    assert _violations(source, "4200")


def test_raw_sql_constants_and_concatenation_are_checked() -> None:
    source = """\
DDL_PREFIX = 'DROP ' + 'TABLE '
DDL = DDL_PREFIX + 'old_table'

def upgrade():
    op.execute(DDL)
"""
    assert _violations(source, "4200")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX old_index",
        "ALTER TABLE items DROP CONSTRAINT old_constraint",
    ],
)
def test_drop_index_and_constraint_are_outside_column_drop_rule(sql: str) -> None:
    source = f"def upgrade():\n    op.execute({sql!r})\n"
    assert _violations(source, "4200") == []


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
    assert _violations(source, "4200") == []


def test_execute_keyword_sqltext_is_checked_and_dynamic_missing_argument_fails_closed() -> (
    None
):
    source = "def upgrade():\n    op.execute(sqltext='DROP TABLE old_table')\n"
    assert _violations(source, "4200")

    missing = "def upgrade():\n    op.execute(encoding='utf-8')\n"
    assert _violations(missing, "4200")

    dynamic = "def upgrade():\n    op.execute(sqltext=build_ddl())\n"
    assert _violations(dynamic, "4200")
    marked = _file_marker(
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    op.execute(sqltext=build_ddl())\n"
    )
    assert _violations(marked, "4200") == []
    multiline = _file_marker(
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    op.execute(\n        sqltext=build_ddl()\n    )\n"
    )
    assert _violations(multiline, "4200") == []


def test_text_wrapper_requires_a_trusted_sqlalchemy_import() -> None:
    trusted = (
        "from sqlalchemy import text\n"
        "def upgrade():\n    op.execute(text('DROP TABLE old'))\n"
    )
    assert _violations(trusted, "4200")
    untrusted = (
        "from helpers import text\n"
        "def upgrade():\n    op.execute(text('DROP TABLE old'))\n"
    )
    assert _violations(untrusted, "4200")


def test_dynamic_raw_sql_fails_closed() -> None:
    source = "def upgrade():\n    op.execute(f'DROP TABLE {table_name}')\n"
    assert any(
        "module shape is indeterminate" in item for item in _violations(source, "4200")
    )


def test_raw_sql_assigned_inside_upgrade_is_checked() -> None:
    source = "def upgrade():\n    statement = 'DROP TABLE old_table'\n    op.execute(statement)\n"
    assert _violations(source, "4200")


def test_reassigned_sql_name_fails_closed_instead_of_reusing_a_literal() -> None:
    source = "def upgrade():\n    sql = 'SELECT 1'\n    sql = build_ddl()\n    op.execute(sql)\n"
    problems = _violations(source, "4200")
    assert any("module shape is indeterminate" in item for item in problems)

    marked = _file_marker(
        "def upgrade():\n    sql = 'SELECT 1'\n"
        "    # expand-contract: contract-step 4200\n    sql = build_ddl()\n"
        "    # expand-contract: contract-step 4200\n    op.execute(sql)\n"
    )
    assert _violations(marked, "4200") == []

    augmented = (
        "def upgrade():\n    sql = 'SELECT 1'\n    sql += suffix\n    op.execute(sql)\n"
    )
    assert _violations(augmented, "4200")


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
    assert _violations(body, "4200")


def test_unknown_helper_can_only_pass_with_local_contract_marker() -> None:
    source = "def upgrade():\n    external_helper()\n"
    assert _violations(_file_marker(source), "4200") == []

    imported = (
        "from helpers import external_helper\n"
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    external_helper()\n"
    )
    assert _violations(_file_marker(imported), "4200") == []


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
    assert any(
        "module shape is indeterminate" in item for item in _violations(source, "4200")
    )
    marked = _file_marker(
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {expression}\n"
    )
    assert _violations(marked, "4200") == []


def test_exec_driver_sql_literal_destructive_sql_requires_marker() -> None:
    source = "def upgrade():\n    conn.exec_driver_sql('ALTER TABLE items DROP COLUMN old')\n"
    assert _violations(source, "4200")
    marked = _file_marker(
        "def upgrade():\n    # expand-contract: contract-step 4200\n"
        "    conn.exec_driver_sql('ALTER TABLE items DROP COLUMN old')\n"
    )
    assert _violations(marked, "4200") == []


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
    assert _violations(source, "4200")
    marked = _file_marker(
        f"def upgrade():\n    # expand-contract: contract-step 4200\n    {operation}\n"
    )
    assert _violations(marked, "4200") == []


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
    assert _violations(source, "4200") == []


def test_loop_operation_is_allowed_only_with_operation_marker() -> None:
    source = (
        "def upgrade():\n    for table in tables:\n"
        "        op.create_table(table, sa.Column('id', sa.Integer()))\n"
    )
    assert _violations(source, "4200")
    marked = (
        "# expand-contract: contract-step 4200\n"
        "def upgrade():\n    for table in tables:\n"
        "        # expand-contract: contract-step 4200\n"
        "        op.create_table(table, sa.Column('id', sa.Integer()))\n"
    )
    assert _violations(marked, "4200") == []

    helper_loop = (
        "def helper():\n    op.create_table('items')\n"
        "def upgrade():\n    for table in tables:\n        helper()\n"
    )
    assert any(
        "module shape is indeterminate" in item
        for item in _violations(helper_loop, "4200")
    )


def test_forbidden_operation_in_downgrade_does_not_fail() -> None:
    source = "def upgrade():\n    pass\n\ndef downgrade():\n    op.drop_table('old')\n"
    assert _violations(source, "4200") == []


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
        '"""Additive migration."""\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = 'additive'\n"
        "down_revision = None\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n    op.add_column('items', sa.Column('extra', sa.String()))\n"
        "def downgrade():\n    pass\n"
    )
    head = _commit(tmp_path, "add additive migration")
    assert lint.changed_migrations(tmp_path, base, head) == [additive]
    assert lint.check_paths(lint.changed_migrations(tmp_path, base, head), "4200") == []

    forbidden = versions / "changed.py"
    forbidden.write_text(
        '"""Forbidden migration."""\n'
        "from alembic import op\n"
        "revision = 'changed'\n"
        "down_revision = 'additive'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n    op.drop_table('changed')\n"
        "def downgrade():\n    pass\n"
    )
    new_head = _commit(tmp_path, "add forbidden migration")
    changed = lint.changed_migrations(tmp_path, head, new_head)
    assert changed == [forbidden]
    assert lint.check_paths(changed, "4200")

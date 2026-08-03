"""The kis_mock signal-ledger migration adds, and only adds.

Static proof, so it runs in CI without a database: the migration module is
parsed and every ``op.*`` call is checked. A schema-mutating operation, or a
DDL call naming any table other than the new one, fails here.

The complementary live proof (upgrade -> downgrade -> upgrade against a scratch
database, diffing the surrounding schema) is recorded in
docs/runbooks/kis-mock-attribution-chain.md; it is not run in CI because the
test database is built by create_all rather than by alembic.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260803_kis_mock_signal_ledger.py"
)
_NEW_TABLE = "kis_mock_signal_ledger"
# Operations that would mutate something that already exists. None may appear.
_MUTATING_OPS = frozenset(
    {
        "alter_column",
        "drop_column",
        "add_column",
        "drop_constraint",
        "drop_table_comment",
        "rename_table",
        "execute",
        "batch_alter_table",
    }
)


def _op_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]


def test_migration_touches_only_the_new_table():
    tree = ast.parse(_MIGRATION.read_text(encoding="utf-8"))
    module_consts = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }

    def _literal(arg: ast.expr) -> str | None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return module_consts.get(arg.id)
        return None

    seen_ops: set[str] = set()
    for call in _op_calls(tree):
        name = call.func.attr
        seen_ops.add(name)
        assert name not in _MUTATING_OPS, (
            f"migration uses op.{name}, which can alter existing schema objects"
        )
        # Every DDL call must name the new table.
        table_args = [_literal(arg) for arg in call.args]
        assert _NEW_TABLE in table_args, (
            f"op.{name} does not target {_NEW_TABLE}: {table_args}"
        )

    assert "create_table" in seen_ops
    assert "drop_table" in seen_ops, "downgrade must be reversible"


def test_migration_chains_onto_the_current_head():
    tree = ast.parse(_MIGRATION.read_text(encoding="utf-8"))
    assigns = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    assert assigns["revision"] == "20260803_kis_mock_signal"
    assert assigns["down_revision"] == "20260802_rob1036_sample_elig"

"""The migration operation scanner, extracted so it can be tested itself.

This started life inline in ``test_migration.py``. A guard that has never been
shown a hostile input is a guard nobody has tested, so it lives here and
``test_migration_phase_guard.py`` feeds it a corpus of migrations that should
be rejected.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

#: Alembic operations that write or rewrite rows.
ROW_DML_OPS = frozenset({"bulk_insert", "execute", "executemany"})

#: Alembic operations that touch an existing schema object.
MUTATION_OPS = frozenset(
    {
        "alter_column",
        "drop_column",
        "add_column",
        "rename_table",
        "drop_constraint",
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
        "create_unique_constraint",
        "alter_table",
        "batch_alter_table",
    }
)

#: What each phase is allowed to do, and nothing else.
UPGRADE_OPS = frozenset({"create_table", "create_index"})
DOWNGRADE_OPS = frozenset({"drop_index", "drop_table"})
ALLOWED_OPS = UPGRADE_OPS | DOWNGRADE_OPS


@dataclass
class ScanResult:
    """What a migration's source was found to do."""

    #: phase name -> ordered op names called in it
    ops_by_phase: dict[str, list[str]] = field(default_factory=dict)
    #: human-readable reasons the source is not acceptable
    offenders: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.offenders


def scan(source: str, *, table: str, schema: str) -> ScanResult:
    """Scan an Alembic migration's source for what it actually does.

    NOTE: this is the *original* implementation, kept verbatim so the corpus
    in ``test_migration_phase_guard.py`` can demonstrate exactly what it lets
    through. It collects direct ``op.<attr>(...)`` calls across the whole
    module, with no notion of phase and no notion of indirection.
    """
    result = ScanResult()
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    used = {node.func.attr for node in calls} - {"f"}
    result.ops_by_phase["<module>"] = [node.func.attr for node in calls]

    if used & ROW_DML_OPS:
        result.offenders.append(f"row DML: {sorted(used & ROW_DML_OPS)}")
    if used & MUTATION_OPS:
        result.offenders.append(f"schema mutation: {sorted(used & MUTATION_OPS)}")
    if not used <= ALLOWED_OPS | {"f"}:
        result.offenders.append(f"unexpected ops: {sorted(used - ALLOWED_OPS)}")
    return result

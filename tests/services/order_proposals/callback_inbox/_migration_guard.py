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
PHASE_OPS = {"upgrade": UPGRADE_OPS, "downgrade": DOWNGRADE_OPS}

#: Anything that hands out a raw connection or executes raw SQL.
ESCAPE_HATCHES = frozenset({"get_bind", "execute", "executemany", "exec_driver_sql"})


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


def _alias_names(tree: ast.AST) -> set[str]:
    """Every name that has been bound to ``op``.

    ``o = op`` and ``o: Operations = op`` both make ``o`` a way to call
    anything on the operations proxy, so the scan has to follow them.
    """
    aliases = {"op"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                value, targets = node.value, list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            if not isinstance(value, ast.Name) or value.id not in aliases:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _enclosing_phase(tree: ast.AST, node: ast.AST) -> str:
    """Which top-level function this node lives in, or ``<module>``."""
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(candidate):
            if inner is node:
                return candidate.name
    return "<module>"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _mentions(node: ast.AST | None, *, wanted: str, constant: str) -> bool:
    """Does this argument resolve to the wanted table/schema?

    Accepts either the literal or a module constant whose name carries it,
    since the real migration uses ``_TABLE``/``_SCHEMA``.
    """
    if node is None:
        return False
    rendered = ast.dump(node)
    return wanted in rendered or constant in rendered


def scan(source: str, *, table: str, schema: str) -> ScanResult:
    """Scan an Alembic migration for what it actually does, per phase.

    Closed-world about *receivers*: only a direct attribute call on ``op`` or
    a name bound to it counts as an operation, and any other way of reaching
    the operations proxy -- ``getattr``, ``get_bind``, raw ``execute``,
    handing ``op`` to a helper, importing from ``alembic.op`` -- is rejected
    outright rather than analysed. There is no legitimate reason for an
    additive migration to do any of them, so refusing is cheaper and safer
    than trying to decide what they do.
    """
    result = ScanResult()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - corpus is valid python
        result.offenders.append(f"unparseable: {exc}")
        return result

    aliases = _alias_names(tree)
    if aliases != {"op"}:
        result.offenders.append(f"`op` is aliased: {sorted(aliases - {'op'})}")

    # Importing the operations directly bypasses receiver analysis entirely.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "alembic.op"
        ):
            result.offenders.append("imports directly from alembic.op")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in aliases
        ):
            result.offenders.append(f"line {node.lineno}: getattr on the op proxy")
        # Handing `op` to anything is handing it the whole API.
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in aliases
        ):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name) and arg.id in aliases:
                    result.offenders.append(
                        f"line {node.lineno}: the op proxy is passed to a callable"
                    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        attr = node.func.attr
        if isinstance(receiver, ast.Name) and receiver.id in aliases:
            if attr == "f":
                continue
            phase = _enclosing_phase(tree, node)
            result.ops_by_phase.setdefault(phase, []).append(attr)
            if attr in ESCAPE_HATCHES:
                result.offenders.append(f"line {node.lineno}: op.{attr} is an escape")
                continue
            if attr in ROW_DML_OPS:
                result.offenders.append(f"line {node.lineno}: row DML op.{attr}")
                continue
            if attr in MUTATION_OPS:
                result.offenders.append(
                    f"line {node.lineno}: schema mutation op.{attr}"
                )
                continue
            if attr not in ALLOWED_OPS:
                result.offenders.append(f"line {node.lineno}: unexpected op.{attr}")
                continue
            allowed_here = PHASE_OPS.get(phase)
            if allowed_here is None:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} outside upgrade/downgrade"
                )
                continue
            if attr not in allowed_here:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} is not allowed in {phase}"
                )
                continue
            # Every allowed DDL call must name this table, in this schema.
            positional = node.args[0] if node.args else None
            table_arg = _keyword(node, "table_name") or (
                node.args[1] if attr == "create_index" and len(node.args) > 1 else None
            )
            names_table = _mentions(
                positional, wanted=table, constant="_TABLE"
            ) or _mentions(table_arg, wanted=table, constant="_TABLE")
            if not names_table:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} does not name {table}"
                )
            schema_arg = _keyword(node, "schema")
            if schema_arg is None:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} is not schema-qualified"
                )
            elif not _mentions(schema_arg, wanted=schema, constant="_SCHEMA"):
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} targets another schema"
                )
        else:
            # A raw connection obtained any other way.
            if attr in {"execute", "executemany", "exec_driver_sql"}:
                result.offenders.append(
                    f"line {node.lineno}: raw {attr} outside the op proxy"
                )

    for phase, ops in result.ops_by_phase.items():
        if phase in PHASE_OPS and not ops:  # pragma: no cover - defensive
            result.offenders.append(f"{phase} does nothing")
    return result

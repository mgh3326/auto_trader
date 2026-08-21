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
RAW_CONNECTION_METHODS = frozenset({"execute", "executemany", "exec_driver_sql"})


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


def _assignment_parts(node: ast.AST) -> tuple[ast.expr | None, list[ast.expr]]:
    """Return the value and simple targets for a normal/annotated assignment."""
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets)
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.value, [node.target]
    return None, []


def _assigned_names(node: ast.AST) -> set[str]:
    """Only ordinary name bindings can later become an indirect call target."""
    _, targets = _assignment_parts(node)
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _method_alias_offenders(tree: ast.AST, *, op_aliases: set[str]) -> list[str]:
    """Reject aliases of an Alembic method or raw connection execution method.

    A call such as ``run = op.execute; run(...)`` has no direct ``op``
    receiver at its call site, so the phase/operation trace alone cannot see
    it. A raw connection has the same shape. This migration has no legitimate
    need to store either kind of method, so rejecting the binding itself is
    safer than attempting whole-program alias resolution.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        value, _ = _assignment_parts(node)
        if not isinstance(value, ast.Attribute):
            continue
        targets = _assigned_names(node)
        if not targets:
            continue
        receiver_is_op = (
            isinstance(value.value, ast.Name) and value.value.id in op_aliases
        )
        raw_connection_method = value.attr in RAW_CONNECTION_METHODS
        if not receiver_is_op and not raw_connection_method:
            continue
        source = (
            f"{value.value.id}.{value.attr}"
            if isinstance(value.value, ast.Name)
            else value.attr
        )
        kind = "op method" if receiver_is_op else "raw connection method"
        for target in sorted(targets):
            offenders.append(
                f"line {node.lineno}: {kind} {source} is aliased as {target}"
            )
    return offenders


def _imported_bindings(tree: ast.AST) -> dict[str, str]:
    """Map every imported local name to its declared module path."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                bindings[local] = f"{module}.{alias.name}".strip(".")
    return bindings


def _is_sqlalchemy_constructor_import(origin: str) -> bool:
    """SQLAlchemy imports construct DDL values; they do not execute a migration."""
    return origin == "sqlalchemy" or origin.startswith("sqlalchemy.")


def _imported_helper_offenders(tree: ast.AST, *, op_aliases: set[str]) -> list[str]:
    """Reject opaque imported helper execution while allowing SQLAlchemy DDL.

    A zero-argument imported helper can mutate through a hidden connection
    without receiving ``op`` at all. The closed operation trace cannot infer
    that side effect, so this additive migration permits direct imported call
    targets only from SQLAlchemy's DDL-constructor namespace. Aliases of an
    opaque imported helper are followed one assignment deep (and transitively)
    before calls are checked.
    """
    imported = _imported_bindings(tree)
    helper_aliases = {
        name
        for name, origin in imported.items()
        if name not in op_aliases and not _is_sqlalchemy_constructor_import(origin)
    }
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value, _ = _assignment_parts(node)
            if not isinstance(value, ast.Name) or value.id not in helper_aliases:
                continue
            for target in _assigned_names(node):
                if target not in helper_aliases:
                    helper_aliases.add(target)
                    changed = True

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in helper_aliases:
            offenders.append(
                f"line {node.lineno}: imported helper {node.func.id} is executed"
            )
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in helper_aliases
        ):
            offenders.append(
                f"line {node.lineno}: imported helper {node.func.value.id}.{node.func.attr} is executed"
            )
    return offenders


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


def _literal_assignments(tree: ast.AST) -> dict[str, str]:
    """Resolve only exact module-level string constants, never name-shaped hints."""
    values: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, ast.Constant) and type(value.value) is str:
            values[target.id] = value.value
    return values


def _resolve_literal(node: ast.AST | None, *, constants: dict[str, str]) -> str | None:
    """Resolve a literal or one of the three approved literal constants.

    ``_TABLE`` used to be accepted merely because its spelling contained the
    expected table name. Only ``_INBOX_TABLE``, ``_CURSOR_TABLE``, and
    ``_SCHEMA`` are legitimate aliases, and each must resolve exactly.
    """
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def scan(
    source: str,
    *,
    inbox_table: str,
    cursor_table: str,
    schema: str,
) -> ScanResult:
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

    expected_constants = {
        "_INBOX_TABLE": inbox_table,
        "_CURSOR_TABLE": cursor_table,
        "_SCHEMA": schema,
    }
    assigned_constants = _literal_assignments(tree)
    constants: dict[str, str] = {}
    for name, expected in expected_constants.items():
        if name not in assigned_constants:
            continue
        actual = assigned_constants[name]
        if actual != expected:
            result.offenders.append(
                f"{name} resolves to {actual!r}, not exact {expected!r}"
            )
        else:
            constants[name] = actual

    aliases = _alias_names(tree)
    if aliases != {"op"}:
        result.offenders.append(f"`op` is aliased: {sorted(aliases - {'op'})}")
    result.offenders.extend(_method_alias_offenders(tree, op_aliases=aliases))
    result.offenders.extend(_imported_helper_offenders(tree, op_aliases=aliases))

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
            if attr in ESCAPE_HATCHES:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(f"line {node.lineno}: op.{attr} is an escape")
                continue
            if attr in ROW_DML_OPS:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(f"line {node.lineno}: row DML op.{attr}")
                continue
            if attr in MUTATION_OPS:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(
                    f"line {node.lineno}: schema mutation op.{attr}"
                )
                continue
            if attr not in ALLOWED_OPS:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(f"line {node.lineno}: unexpected op.{attr}")
                continue
            allowed_here = PHASE_OPS.get(phase)
            if allowed_here is None:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} outside upgrade/downgrade"
                )
                continue
            if attr not in allowed_here:
                result.ops_by_phase.setdefault(phase, []).append(attr)
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} is not allowed in {phase}"
                )
                continue
            # The two-table migration has an exact closed operation trace.
            # Resolve values, never substring-match an AST dump or a generic
            # ``_TABLE`` identifier.
            table_arg = _keyword(node, "table_name")
            if table_arg is None:
                if attr == "create_index":
                    table_arg = node.args[1] if len(node.args) > 1 else None
                else:
                    table_arg = node.args[0] if node.args else None
            table_name = _resolve_literal(table_arg, constants=constants)
            if table_name not in {inbox_table, cursor_table}:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} targets an unknown table"
                )
            schema_arg = _keyword(node, "schema")
            if schema_arg is None:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} is not schema-qualified"
                )
            elif _resolve_literal(schema_arg, constants=constants) != schema:
                result.offenders.append(
                    f"line {node.lineno}: op.{attr} targets another schema"
                )
            if attr in {"create_index", "drop_index"}:
                index_arg = node.args[0] if node.args else None
                if _resolve_literal(index_arg, constants=constants) != (
                    "ix_telegram_callback_inbox_state_available"
                ):
                    result.offenders.append(
                        f"line {node.lineno}: op.{attr} uses an unexpected index"
                    )
                if table_name != inbox_table:
                    result.offenders.append(
                        f"line {node.lineno}: cursor table may not carry an index"
                    )
            result.ops_by_phase.setdefault(phase, []).append(f"{attr}:{table_name}")
        else:
            # A raw connection obtained any other way.
            if attr in {"execute", "executemany", "exec_driver_sql"}:
                result.offenders.append(
                    f"line {node.lineno}: raw {attr} outside the op proxy"
                )

    expected_trace = {
        "upgrade": [
            f"create_table:{inbox_table}",
            f"create_index:{inbox_table}",
            f"create_table:{cursor_table}",
        ],
        "downgrade": [
            f"drop_table:{cursor_table}",
            f"drop_index:{inbox_table}",
            f"drop_table:{inbox_table}",
        ],
    }
    for phase, expected in expected_trace.items():
        actual = result.ops_by_phase.get(phase, [])
        if actual != expected:
            result.offenders.append(
                f"{phase} trace is {actual}, expected exactly {expected}"
            )
    if set(result.ops_by_phase) - set(expected_trace):
        result.offenders.append("operations appear outside the two migration phases")
    return result

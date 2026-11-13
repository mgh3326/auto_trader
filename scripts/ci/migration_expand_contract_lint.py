"""Check changed Alembic migrations for expand-contract violations."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

CONTRACT_MARKER_RE = re.compile(r"^\s*# expand-contract: contract-step ([0-9]+)\s*$")
TASK_ID_RE = re.compile(r"[0-9]+")
RAW_SQL_RE = re.compile(
    r"\bDROP\s+TABLE\b"
    r"|\bDROP\s+COLUMN\b"
    r"|\bALTER\s+TABLE\b[\s\S]*?\bDROP\s+(?:COLUMN\s+)?"
    r"(?!CONSTRAINT\b|INDEX\b|PRIMARY\b|FOREIGN\b|UNIQUE\b|CHECK\b|NOT\s+NULL\b|DEFAULT\b|IDENTITY\b)"
    r"[\"`A-Z_][\w$\"`]*"
    r"|\bRENAME\b"
    r"|\bSET\s+NOT\s+NULL\b"
    r"|\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+(?:COLUMN\s+)?"
    r"[\"`A-Z_][\w$\"`]*\s+(?:SET\s+)?NOT\s+NULL\b"
    r"|\bALTER\s+TABLE\b[\s\S]*?\bALTER\s+(?:COLUMN\s+)?"
    r"[\"`A-Z_][\w$\"`]*\s+(?:SET\s+DATA\s+)?TYPE\b",
    re.I,
)
DESTRUCTIVE_OPS = frozenset({"drop_table", "drop_column", "rename_table"})
SQL_EXECUTION_METHODS = frozenset({"execute", "exec_driver_sql"})
EXPANSION_OPS = frozenset(
    {
        "add_column",
        "alter_column",
        "batch_alter_table",
        "create_check_constraint",
        "create_exclude_constraint",
        "create_foreign_key",
        "create_index",
        "create_primary_key",
        "create_sequence",
        "create_table",
        "create_table_comment",
        "create_unique_constraint",
        "f",
        "get_bind",
        "inline_literal",
    }
)
SAFE_BUILTIN_CALLS = frozenset(
    {
        "RuntimeError",
        "ValueError",
        "TypeError",
        "any",
        "all",
        "bool",
        "dict",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "set",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)
SAFE_SQLALCHEMY_CALLS = frozenset(
    {
        "ARRAY",
        "BigInteger",
        "Boolean",
        "CHAR",
        "CheckConstraint",
        "Column",
        "Date",
        "DateTime",
        "DOUBLE_PRECISION",
        "ENUM",
        "Enum",
        "Float",
        "ForeignKey",
        "ForeignKeyConstraint",
        "Identity",
        "INTEGER",
        "Integer",
        "Interval",
        "JSON",
        "JSONB",
        "Numeric",
        "PrimaryKeyConstraint",
        "SmallInteger",
        "String",
        "TEXT",
        "TIMESTAMP",
        "Text",
        "UniqueConstraint",
        "Uuid",
        "UUID",
        "VARCHAR",
        "false",
        "column",
        "inspect",
        "now",
        "table",
        "text",
    }
)


def _reachable_upgrade_functions(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "upgrade" not in functions:
        return []

    reachable: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    pending = ["upgrade"]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        function = functions.get(name)
        if function is None:
            continue
        reachable.append(function)
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in functions
                and node.func.id not in visited
            ):
                pending.append(node.func.id)
    return reachable


def _upgrade_entrypoint_problem(tree: ast.Module) -> str | None:
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "upgrade"
    ]
    nested_definitions = [
        node
        for statement in tree.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(statement)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "upgrade"
    ]
    reassigned = [
        node
        for statement in tree.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(statement)
        if isinstance(node, ast.Name)
        and node.id == "upgrade"
        and isinstance(node.ctx, ast.Store)
    ]
    if len(definitions) != 1:
        return "migration must define one top-level upgrade() function"
    if (
        nested_definitions
        or reassigned
        or isinstance(definitions[0], ast.AsyncFunctionDef)
    ):
        return "upgrade must be a plain top-level function definition"
    return None


def _module_level_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return calls


def _safe_imported_calls(tree: ast.Module) -> set[str]:
    imports: dict[str, list[tuple[str, str]]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imports.setdefault(alias.asname or alias.name, []).append(
                        (node.module or "", alias.name)
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.setdefault(alias.asname or alias.name.split(".")[0], []).append(
                    (alias.name, "")
                )
    safe_modules = {"sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression"}
    safe = {
        name
        for name, origins in imports.items()
        if len(origins) == 1
        and origins[0][0] in safe_modules
        and origins[0][1] in {"inspect", "text"}
    }
    reassigned = {
        name.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        for name in ast.walk(target)
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
    }
    return safe - reassigned


def _module_assigned_names(tree: ast.Module) -> set[str]:
    return {
        node.id
        for statement in tree.body
        if not isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def _literal_sql(node: ast.AST, imported_calls: set[str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "text"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"sa", "sqlalchemy"}
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in imported_calls
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _batch_receiver_names(
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
) -> set[str]:
    candidates: set[str] = set()
    for function in functions:
        for node in ast.walk(function):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    context = item.context_expr
                    if (
                        isinstance(context, ast.Call)
                        and isinstance(context.func, ast.Attribute)
                        and context.func.attr == "batch_alter_table"
                        and isinstance(context.func.value, ast.Name)
                        and context.func.value.id == "op"
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        candidates.add(item.optional_vars.id)
    return {
        name
        for name in candidates
        if sum(
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Store)
            for function in functions
            for node in ast.walk(function)
        )
        == 1
    }


def _has_contract_marker(
    source_lines: list[str],
    call: ast.Call,
    task_id: str,
    parents: dict[ast.AST, ast.AST],
    batch_names: set[str],
) -> bool:
    covered = call
    parent = parents.get(call)
    while parent is not None:
        if isinstance(parent, ast.Call):
            method = parent.func.attr if isinstance(parent.func, ast.Attribute) else ""
            root = (
                _root_name(parent.func.value)
                if isinstance(parent.func, ast.Attribute)
                else None
            )
            if method in SQL_EXECUTION_METHODS or (
                method in EXPANSION_OPS | DESTRUCTIVE_OPS | {"alter_column"}
                and root in {"op", *batch_names}
            ):
                covered = parent
        parent = parents.get(parent)
    marker_line = covered.lineno - 2
    if marker_line < 0:
        return False
    match = CONTRACT_MARKER_RE.fullmatch(source_lines[marker_line])
    return match is not None and match.group(1) == task_id


def _argument(
    call: ast.Call, keywords: dict[str | None, ast.AST], name: str, position: int
) -> ast.AST | None:
    if len(call.args) > position:
        return call.args[position]
    return keywords.get(name)


def _alter_column_problem(call: ast.Call) -> str | None:
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return "alter_column uses dynamic *args"
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if None in keywords:
        return "alter_column uses dynamic **kwargs"
    allowed_keywords = {
        "schema",
        "table_name",
        "column_name",
        "nullable",
        "comment",
        "server_default",
        "new_column_name",
        "type_",
        "existing_type",
        "existing_server_default",
        "existing_nullable",
        "existing_comment",
        "existing_autoincrement",
        "autoincrement",
        "quote",
    }
    if any(name not in allowed_keywords for name in keywords):
        return "alter_column uses an unsupported keyword"
    if len(call.args) > 7:
        return "alter_column has positional arguments outside the checked signature"

    nullable = _argument(call, keywords, "nullable", 2)
    if nullable is not None:
        if not isinstance(nullable, ast.Constant) or (
            nullable.value is not None and type(nullable.value) is not bool
        ):
            return "alter_column nullable value cannot be checked statically"
        if nullable.value is False:
            return "alter_column tightens a column to NOT NULL"

    new_name = _argument(call, keywords, "new_column_name", 5)
    if new_name is not None and not (
        isinstance(new_name, ast.Constant) and new_name.value is None
    ):
        return "alter_column renames a column or uses a dynamic new_column_name"

    type_value = _argument(call, keywords, "type_", 6)
    if type_value is not None and not (
        isinstance(type_value, ast.Constant) and type_value.value is None
    ):
        return "alter_column changes a column type or uses a dynamic type_"
    return None


def violations(source: str, task_id: str) -> list[str]:
    """Check only the closed, statically safe migration grammar."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno or 1}: migration is not valid Python: {exc.msg}"]

    source_lines = source.splitlines()
    entrypoint_problem = _upgrade_entrypoint_problem(tree)
    if entrypoint_problem:
        return [f"line 1: {entrypoint_problem}"]
    functions = _reachable_upgrade_functions(tree)
    if not functions:
        return ["line 1: upgrade() has no statically reachable function body"]
    module_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    safe_imported_calls = _safe_imported_calls(tree)
    module_shadowed_names = _module_assigned_names(tree)
    for function in functions:
        module_shadowed_names.update(
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in {"op", "sa", "sqlalchemy", "postgresql"}
        )
        safe_imported_calls.difference_update(
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        )
    batch_names = _batch_receiver_names(functions)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    found: list[str] = []

    for call in _module_level_calls(tree):
        if not _has_contract_marker(source_lines, call, task_id, parents, batch_names):
            found.append(
                f"line {call.lineno}: module-level call cannot be checked statically"
            )

    for function in functions:
        shadowed_names = {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        shadowed_names.update(
            argument.arg
            for argument in [
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            ]
        )
        if function.args.vararg is not None:
            shadowed_names.add(function.args.vararg.arg)
        if function.args.kwarg is not None:
            shadowed_names.add(function.args.kwarg.arg)
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                shadowed_names.update(
                    alias.asname or alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                shadowed_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name != "*"
                )
        loop_calls = {
            id(child)
            for loop in ast.walk(function)
            if isinstance(loop, (ast.For, ast.AsyncFor, ast.While))
            for child in ast.walk(loop)
            if isinstance(child, ast.Call)
        }
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            root = (
                _root_name(call.func.value)
                if isinstance(call.func, ast.Attribute)
                else None
            )
            message: str | None = None

            if root in {"op", "sa", "sqlalchemy", "postgresql"} and (
                root in shadowed_names or root in module_shadowed_names
            ):
                message = f"namespace {root!r} is rebound inside upgrade"
            elif method in SQL_EXECUTION_METHODS:
                sql_argument = (
                    call.args[0]
                    if call.args
                    else next(
                        (
                            keyword.value
                            for keyword in call.keywords
                            if keyword.arg in {"sqltext", "statement"}
                        ),
                        None,
                    )
                )
                sql = (
                    _literal_sql(sql_argument, safe_imported_calls)
                    if sql_argument is not None
                    else None
                )
                if sql is None:
                    message = f"{method} SQL is not a string literal"
                else:
                    match = RAW_SQL_RE.search(sql)
                    if match:
                        message = (
                            f"{method} contains destructive SQL ({match.group(0)})"
                        )
            elif method in DESTRUCTIVE_OPS:
                message = f"{method} is a destructive schema operation"
            elif method == "alter_column":
                if root not in {"op", *batch_names}:
                    message = "alter_column receiver cannot be checked statically"
                else:
                    message = _alter_column_problem(call)
            elif isinstance(call.func, ast.Name):
                name = call.func.id
                if name in shadowed_names:
                    message = f"call target {name!r} is rebound inside upgrade"
                elif (
                    name not in module_functions
                    and name not in SAFE_BUILTIN_CALLS
                    and name not in safe_imported_calls
                ):
                    message = (
                        f"call to unknown helper {name!r} cannot be checked statically"
                    )
            elif root in {"op", *batch_names}:
                if method not in EXPANSION_OPS:
                    message = (
                        f"operation {method!r} is not in the expand-only allowlist"
                    )
            elif root in {"sa", "sqlalchemy", "postgresql"}:
                if method not in SAFE_SQLALCHEMY_CALLS:
                    message = f"call {method!r} is outside the SQLAlchemy constructor allowlist"
            elif root == "datetime":
                if method not in {
                    "date",
                    "datetime",
                    "time",
                    "timedelta",
                    "fromisoformat",
                }:
                    message = f"call {method!r} is outside the datetime allowlist"
            else:
                message = f"call {method or '<dynamic>'!r} cannot be checked statically"

            if message is None and id(call) in loop_calls:
                if (
                    method in EXPANSION_OPS
                    or method in SQL_EXECUTION_METHODS
                    or method in DESTRUCTIVE_OPS
                ):
                    message = (
                        "migration operation inside a loop cannot be checked statically"
                    )
                elif (
                    isinstance(call.func, ast.Name) and call.func.id in module_functions
                ):
                    message = "helper call inside a loop cannot be checked statically"
            if message and not _has_contract_marker(
                source_lines, call, task_id, parents, batch_names
            ):
                found.append(f"line {call.lineno}: {message}")
    return found


def changed_migrations(root: Path, base_ref: str, head_ref: str) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            base_ref,
            head_ref,
            "--",
            "alembic/versions/*.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [root / name for name in result.stdout.splitlines() if name]


def check_paths(paths: list[Path], task_id: str) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path}: cannot read migration: {exc}")
            continue
        errors.extend(f"{path}: {problem}" for problem in violations(source, task_id))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref", required=True, help="base commit/ref for the PR diff"
    )
    parser.add_argument(
        "--head-ref", default="HEAD", help="head commit/ref (default: HEAD)"
    )
    parser.add_argument(
        "--task-id", required=True, help="Mergewake-Task PR trailer value"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not TASK_ID_RE.fullmatch(args.task_id):
        parser.error("--task-id must contain only decimal digits")
    try:
        paths = changed_migrations(args.root, args.base_ref, args.head_ref)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or str(exc)
        print(
            f"migration expand-contract lint: cannot read git diff: {detail}",
            file=sys.stderr,
        )
        return 2

    errors = check_paths(paths, args.task_id)
    if errors:
        print("migration expand-contract lint failed:")
        print("\n".join(errors))
        print(
            "Place the matching contract-step marker directly above each approved contract operation."
        )
        return 1
    print(f"migration expand-contract lint passed ({len(paths)} changed migration(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

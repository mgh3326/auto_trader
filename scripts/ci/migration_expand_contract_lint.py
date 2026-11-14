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
        "now",
        "table",
        "text",
    }
)
SAFE_TYPING_IMPORTS = frozenset({"Sequence", "Union"})
SAFE_COLLECTION_IMPORTS = frozenset(
    {"Callable", "Collection", "Iterable", "Iterator", "Mapping", "Sequence"}
)
MODULE_METADATA = frozenset(
    {"revision", "down_revision", "branch_labels", "depends_on"}
)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _matching_file_marker(
    source_lines: list[str], tree: ast.Module, task_id: str
) -> bool:
    first_non_docstring = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        first_non_docstring = 1
    header_end = (
        tree.body[first_non_docstring].lineno - 1
        if len(tree.body) > first_non_docstring
        else len(source_lines)
    )
    return any(
        (match := CONTRACT_MARKER_RE.fullmatch(line)) is not None
        and match.group(1) == task_id
        for line in source_lines[:header_end]
    )


def _function_shape_problem(node: ast.FunctionDef) -> str | None:
    if node.decorator_list:
        return f"{node.name}() has decorators"
    if (
        node.args.posonlyargs
        or node.args.args
        or node.args.kwonlyargs
        or node.args.vararg
        or node.args.kwarg
        or node.args.defaults
        or any(default is not None for default in node.args.kw_defaults)
    ):
        return f"{node.name}() has parameters or defaults"
    if node.returns is not None and not (
        isinstance(node.returns, ast.Constant) and node.returns.value is None
    ):
        return f"{node.name}() has a return annotation other than None"
    if node.type_comment or getattr(node, "type_params", ()):
        return f"{node.name}() has a type comment or type parameters"
    return None


def _allowed_import(node: ast.Import | ast.ImportFrom) -> tuple[bool, set[str]]:
    if isinstance(node, ast.Import):
        if not node.names:
            return False, set()
        bound: set[str] = set()
        for alias in node.names:
            if alias.name == "sqlalchemy" and alias.asname == "sa":
                bound.add("sa")
            elif alias.name.startswith("sqlalchemy.dialects.") and alias.asname:
                bound.add(alias.asname)
            else:
                return False, set()
        return True, bound

    module = node.module or ""
    names = {alias.name for alias in node.names}
    bound = {alias.asname or alias.name for alias in node.names}
    if "*" in names or len(names) != len(node.names):
        return False, set()
    if module == "__future__":
        return names == {"annotations"}, bound
    if module == "alembic":
        return bool(names) and names <= {"op", "context"}, bound
    if module == "typing":
        return bool(names) and names <= SAFE_TYPING_IMPORTS, bound
    if module == "collections.abc":
        return bool(names) and names <= SAFE_COLLECTION_IMPORTS, bound
    if module == "sqlalchemy" or module.startswith("sqlalchemy."):
        return bool(names), bound
    return False, set()


def _annotation_problem(node: ast.AST, imported_names: set[str]) -> str | None:
    if isinstance(node, ast.Constant):
        return (
            None
            if node.value is None
            else "metadata annotation is not a type expression"
        )
    if isinstance(node, ast.Name):
        if node.id in imported_names | {"str", "int", "bool", "float", "bytes"}:
            return None
        return f"metadata annotation name {node.id!r} is not imported"
    if isinstance(node, ast.Attribute):
        root = _root_name(node)
        return (
            None
            if root in imported_names
            else "metadata annotation namespace is not imported"
        )
    if isinstance(node, ast.Subscript):
        for child in (node.value, node.slice):
            problem = _annotation_problem(child, imported_names)
            if problem:
                return problem
        return None
    if isinstance(node, ast.Tuple):
        for child in node.elts:
            problem = _annotation_problem(child, imported_names)
            if problem:
                return problem
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for child in (node.left, node.right):
            problem = _annotation_problem(child, imported_names)
            if problem:
                return problem
        return None
    return "metadata annotation is outside the literal type grammar"


def _module_shape_problem(tree: ast.Module) -> str | None:
    functions: dict[str, ast.FunctionDef] = {}
    metadata: dict[str, ast.AST] = {}
    imported_names: set[str] = set()
    sqlalchemy_names: set[str] = set()
    safe_constructor_names: set[str] = set()
    has_docstring = bool(
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    )
    function_phase = False

    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if function_phase:
                return f"line {node.lineno}: import appears after migration functions"
            allowed, names = _allowed_import(node)
            if not allowed:
                return f"line {node.lineno}: import is outside the migration allowlist"
            if imported_names & names:
                return f"line {node.lineno}: imported name is rebound"
            imported_names.update(names)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlalchemy":
                        sqlalchemy_names.add(alias.asname or "sqlalchemy")
                    elif alias.name.startswith("sqlalchemy."):
                        sqlalchemy_names.add(alias.asname or alias.name.split(".")[0])
            else:
                module = node.module or ""
                if module == "sqlalchemy" or module.startswith("sqlalchemy."):
                    sqlalchemy_names.update(names)
                    safe_constructor_names.update(
                        bound_name
                        for alias in node.names
                        if alias.name in SAFE_SQLALCHEMY_CALLS
                        for bound_name in (alias.asname or alias.name,)
                    )
            continue
        if isinstance(node, ast.Assign):
            if function_phase:
                return (
                    f"line {node.lineno}: assignment appears after migration functions"
                )
            if (
                len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
                or node.targets[0].id not in MODULE_METADATA
            ):
                return f"line {node.lineno}: only migration metadata assignments are allowed"
            name = node.targets[0].id
            if name in metadata:
                return (
                    f"line {node.lineno}: migration metadata {name!r} is assigned twice"
                )
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                return f"line {node.lineno}: migration metadata {name!r} is not literal"
            metadata[name] = node.value
            continue
        if isinstance(node, ast.AnnAssign):
            if function_phase:
                return (
                    f"line {node.lineno}: assignment appears after migration functions"
                )
            if (
                not isinstance(node.target, ast.Name)
                or node.target.id not in MODULE_METADATA
                or node.value is None
            ):
                return (
                    f"line {node.lineno}: only valued metadata annotations are allowed"
                )
            name = node.target.id
            if name in metadata:
                return (
                    f"line {node.lineno}: migration metadata {name!r} is assigned twice"
                )
            annotation_problem = _annotation_problem(node.annotation, imported_names)
            if annotation_problem:
                return f"line {node.lineno}: {annotation_problem}"
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                return f"line {node.lineno}: migration metadata {name!r} is not literal"
            metadata[name] = node.value
            continue
        if isinstance(node, ast.FunctionDef):
            function_phase = True
            if node.name not in {"upgrade", "downgrade"} or node.name in functions:
                return f"line {node.lineno}: only one upgrade() and downgrade() are allowed"
            problem = _function_shape_problem(node)
            if problem:
                return f"line {node.lineno}: {problem}"
            functions[node.name] = node
            continue
        return f"line {getattr(node, 'lineno', 1)}: {type(node).__name__} is outside the module grammar"

    if not has_docstring:
        return "migration must start with a module docstring"
    if set(functions) != {"upgrade", "downgrade"}:
        return "migration must define exactly upgrade() and downgrade()"
    if metadata.keys() != MODULE_METADATA:
        return "all four migration metadata fields must be literal assignments"
    if "op" not in imported_names:
        return "migration must use the allowed 'from alembic import op' import"
    return _upgrade_body_problem(
        functions["upgrade"], imported_names, sqlalchemy_names, safe_constructor_names
    )


def _expression_problem(
    node: ast.AST,
    imported_names: set[str],
    sqlalchemy_names: set[str],
    safe_constructor_names: set[str],
    batch_name: str | None = None,
    allow_op_operation: bool = False,
    allow_batch_context: bool = False,
) -> str | None:
    if isinstance(node, ast.Constant):
        if node.value is None or type(node.value) in {bool, int, float, str, bytes}:
            return None
        return "literal value is outside the migration grammar"
    if isinstance(node, ast.Name):
        return f"name {node.id!r} cannot be checked statically"
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        children = node.elts
    elif isinstance(node, ast.Dict):
        children = [child for child in [*node.keys, *node.values] if child is not None]
    elif isinstance(node, ast.Attribute):
        root = _root_name(node)
        if (
            isinstance(node.value, ast.Name)
            and root in sqlalchemy_names
            and node.attr in SAFE_SQLALCHEMY_CALLS
        ):
            return None
        return "attribute value is outside the SQLAlchemy constructor grammar"
    elif isinstance(node, ast.Call):
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if (
            allow_op_operation
            and method in SQL_EXECUTION_METHODS
            and isinstance(node.func, ast.Attribute)
        ):
            return None
        if any(isinstance(argument, ast.Starred) for argument in node.args):
            return "starred arguments cannot be checked statically"
        if any(keyword.arg is None for keyword in node.keywords):
            return "expanded keyword arguments cannot be checked statically"
        root = _root_name(node.func) if isinstance(node.func, ast.Attribute) else None
        if root in sqlalchemy_names:
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == root
                and method in SAFE_SQLALCHEMY_CALLS
            ):
                return "SQLAlchemy call is outside the constructor allowlist"
        elif root == "op":
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "op"
            ):
                return "indirect op attribute call cannot be checked statically"
            if method == "batch_alter_table":
                if not allow_batch_context:
                    return "batch_alter_table must use the closed with-statement form"
            elif method in {"f", "inline_literal"}:
                pass
            elif not (
                allow_op_operation
                and method in EXPANSION_OPS | DESTRUCTIVE_OPS | SQL_EXECUTION_METHODS
            ):
                return "nested op calls are outside the argument grammar"
        elif root == batch_name and batch_name is not None:
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == batch_name
                and allow_op_operation
                and method in EXPANSION_OPS | DESTRUCTIVE_OPS
            ):
                return "batch operation is outside the closed call grammar"
        elif isinstance(node.func, ast.Name) and node.func.id in safe_constructor_names:
            pass
        else:
            return "function call is outside the migration constructor grammar"
        children = [*node.args, *(keyword.value for keyword in node.keywords)]
    else:
        return f"{type(node).__name__} is outside the migration expression grammar"

    for child in children:
        problem = _expression_problem(
            child,
            imported_names,
            sqlalchemy_names,
            safe_constructor_names,
            batch_name=batch_name,
        )
        if problem:
            return problem
    return None


def _upgrade_body_problem(
    function: ast.FunctionDef,
    imported_names: set[str],
    sqlalchemy_names: set[str],
    safe_constructor_names: set[str],
) -> str | None:
    for index, statement in enumerate(function.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            problem = _expression_problem(
                statement.value,
                imported_names,
                sqlalchemy_names,
                safe_constructor_names,
                allow_op_operation=True,
            )
            if problem:
                return f"line {statement.lineno}: {problem}"
            continue
        if isinstance(statement, ast.With) and len(statement.items) == 1:
            item = statement.items[0]
            context = item.context_expr
            alias = item.optional_vars
            if (
                not isinstance(context, ast.Call)
                or not isinstance(context.func, ast.Attribute)
                or context.func.attr != "batch_alter_table"
                or not isinstance(context.func.value, ast.Name)
                or context.func.value.id != "op"
                or not isinstance(alias, ast.Name)
            ):
                return f"line {statement.lineno}: with statement is outside the batch grammar"
            context_problem = _expression_problem(
                context,
                imported_names,
                sqlalchemy_names,
                safe_constructor_names,
                allow_op_operation=True,
                allow_batch_context=True,
            )
            if context_problem:
                return f"line {statement.lineno}: {context_problem}"
            for body_statement in statement.body:
                if not (
                    isinstance(body_statement, ast.Expr)
                    and isinstance(body_statement.value, ast.Call)
                ):
                    return f"line {body_statement.lineno}: batch body must contain op calls"
                problem = _expression_problem(
                    body_statement.value,
                    imported_names,
                    sqlalchemy_names,
                    safe_constructor_names,
                    batch_name=alias.id,
                    allow_op_operation=True,
                )
                if problem:
                    return f"line {body_statement.lineno}: {problem}"
            continue
        return (
            f"line {statement.lineno}: upgrade body must contain only allowed op calls"
        )
    return None


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
    nullable = call.args[2] if len(call.args) > 2 else keywords.get("nullable")
    if nullable is not None:
        if not isinstance(nullable, ast.Constant) or (
            nullable.value is not None and type(nullable.value) is not bool
        ):
            return "alter_column nullable value cannot be checked statically"
        if nullable.value is False:
            return "alter_column tightens a column to NOT NULL"
    new_name = call.args[5] if len(call.args) > 5 else keywords.get("new_column_name")
    if new_name is not None and not (
        isinstance(new_name, ast.Constant) and new_name.value is None
    ):
        return "alter_column renames a column or uses a dynamic new_column_name"
    type_value = call.args[6] if len(call.args) > 6 else keywords.get("type_")
    if type_value is not None and not (
        isinstance(type_value, ast.Constant) and type_value.value is None
    ):
        return "alter_column changes a column type or uses a dynamic type_"
    return None


def _operation_problem(call: ast.Call) -> str | None:
    method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    if method in SQL_EXECUTION_METHODS:
        return f"{method} raw SQL execution requires a contract marker"
    if method in DESTRUCTIVE_OPS:
        return f"{method} is a destructive schema operation"
    if method == "alter_column":
        return _alter_column_problem(call)
    return None


def violations(source: str, task_id: str) -> list[str]:
    """Check a closed module grammar before inspecting migration operations."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno or 1}: migration is not valid Python: {exc.msg}"]

    lines = source.splitlines()
    shape_problem = _module_shape_problem(tree)
    if shape_problem:
        if _matching_file_marker(lines, tree, task_id):
            return []
        return [
            f"line 1: migration module shape is indeterminate: {shape_problem}; contract marker required"
        ]

    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    batch_names = {
        item.optional_vars.id
        for statement in upgrade.body
        if isinstance(statement, ast.With)
        for item in statement.items
        if isinstance(item.optional_vars, ast.Name)
    }
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    found: list[str] = []
    for call in (node for node in ast.walk(upgrade) if isinstance(node, ast.Call)):
        message = _operation_problem(call)
        root = (
            _root_name(call.func.value)
            if isinstance(call.func, ast.Attribute)
            else None
        )
        method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        if (
            root in {"op", *batch_names}
            and method in EXPANSION_OPS | DESTRUCTIVE_OPS | SQL_EXECUTION_METHODS
        ):
            if method not in DESTRUCTIVE_OPS | SQL_EXECUTION_METHODS | {"alter_column"}:
                continue
        if message and not _has_contract_marker(
            lines, call, task_id, parents, batch_names
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

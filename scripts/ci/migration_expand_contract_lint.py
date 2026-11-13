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
    r"[\"`A-Z_][\w$\"`]*\s+(?:SET\s+DATA\s+)?TYPE\b",
    re.I,
)
DESTRUCTIVE_OPS = frozenset(
    {"drop_table", "drop_column", "rename_table", "alter_column"}
)


def _string_value(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        return None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_value(node.left, constants)
        right = _string_value(node.right, constants)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        chunks: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                chunks.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                if isinstance(value.value, ast.Constant):
                    chunks.append(str(value.value.value))
                elif isinstance(value.value, ast.Name):
                    resolved = constants.get(value.value.id)
                    if resolved is None:
                        return None
                    chunks.append(resolved)
                else:
                    return None
            else:
                return None
        return "".join(chunks)
    return None


def _static_sql_expression(node: ast.AST, constants: dict[str, str]) -> bool:
    if _string_value(node, constants) is not None:
        return True
    if isinstance(node, ast.Call):
        function = node.func
        is_text_wrapper = (
            isinstance(function, ast.Name) and function.id == "text"
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr == "text"
            and isinstance(function.value, ast.Name)
            and function.value.id in {"sa", "sqlalchemy"}
        )
        return (
            is_text_wrapper
            and bool(node.args)
            and all(
                _static_sql_expression(argument, constants) for argument in node.args
            )
        )
    return False


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if value is None:
            continue
        resolved = _string_value(value, constants)
        if resolved is not None:
            for target in targets:
                for name in _target_names(target):
                    constants[name] = resolved
    return constants


def _function_string_constants(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    module_constants: dict[str, str],
) -> dict[str, str]:
    constants = module_constants.copy()
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    # A short fixed-point pass resolves simple local aliases independent of the
    # AST walk order, while keeping SQL evaluation intentionally static.
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment in assignments:
            value = assignment.value
            if value is None:
                continue
            resolved = _string_value(value, constants)
            if resolved is None:
                continue
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for target in targets:
                for name in _target_names(target):
                    if constants.get(name) != resolved:
                        constants[name] = resolved
                        changed = True
        if not changed:
            break
    return constants


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


def _sql_strings(node: ast.AST, constants: dict[str, str]) -> list[str]:
    resolved = _string_value(node, constants)
    if resolved is not None:
        return [resolved]
    if isinstance(node, ast.Call):
        children = [*node.args, *(keyword.value for keyword in node.keywords)]
    elif isinstance(node, ast.BinOp):
        children = [node.left, node.right]
    elif isinstance(node, ast.JoinedStr):
        children = node.values
    elif isinstance(node, ast.FormattedValue):
        children = [node.value]
    else:
        children = []
    return [value for child in children for value in _sql_strings(child, constants)]


def _has_contract_marker(source_lines: list[str], call: ast.Call, task_id: str) -> bool:
    marker_line = call.lineno - 2
    if marker_line < 0:
        return False
    match = CONTRACT_MARKER_RE.fullmatch(source_lines[marker_line])
    return match is not None and match.group(1) == task_id


def violations(source: str, task_id: str) -> list[str]:
    """Return unsafe upgrade operations lacking their matching local marker."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno or 1}: migration is not valid Python: {exc.msg}"]

    source_lines = source.splitlines()
    module_constants = _module_string_constants(tree)
    found: list[str] = []
    for function in _reachable_upgrade_functions(tree):
        constants = _function_string_constants(function, module_constants)
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            message: str | None = None
            if method in DESTRUCTIVE_OPS:
                keywords = {keyword.arg: keyword.value for keyword in call.keywords}
                if method == "alter_column":
                    if "new_column_name" in keywords and not (
                        isinstance(keywords["new_column_name"], ast.Constant)
                        and keywords["new_column_name"].value is None
                    ):
                        message = "alter_column renames a column"
                    elif "type_" in keywords:
                        message = "alter_column changes a column type"
                    elif (
                        isinstance(keywords.get("nullable"), ast.Constant)
                        and keywords["nullable"].value is False
                    ):
                        message = "alter_column tightens a column to NOT NULL"
                    elif (
                        len(call.args) > 2
                        and isinstance(call.args[2], ast.Constant)
                        and call.args[2].value is False
                    ):
                        message = "alter_column tightens a column to NOT NULL"
                elif method == "drop_table":
                    message = "drop_table removes a table"
                elif method == "drop_column":
                    message = "drop_column removes a column"
                elif method == "rename_table":
                    message = "rename_table renames a table"
            elif method == "execute":
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
                if sql_argument is None or not _static_sql_expression(
                    sql_argument, constants
                ):
                    message = "execute uses SQL that cannot be checked statically"
                else:
                    sql = _sql_strings(sql_argument, constants)
                    match = next(
                        (
                            match
                            for value in sql
                            if (match := RAW_SQL_RE.search(value)) is not None
                        ),
                        None,
                    )
                    if match:
                        message = f"execute contains destructive SQL ({match.group(0)})"

            if message and not _has_contract_marker(source_lines, call, task_id):
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

"""Check changed Alembic migrations for expand-contract violations."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

CONTRACT_MARKER_RE = re.compile(r"^\s*# expand-contract: contract-step [0-9]+\s*$")
RAW_SQL_RE = re.compile(
    r"\bDROP\s+(?:TABLE|COLUMN)\b|\bRENAME\b|\bSET\s+NOT\s+NULL\b", re.I
)


def _is_op_call(call: ast.Call, method: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == method
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "op"
    )


def _string_values(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _upgrade_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "upgrade"
        ):
            calls.extend(
                child for child in ast.walk(node) if isinstance(child, ast.Call)
            )
    return calls


def _upgrade_string_assignments(tree: ast.Module) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in tree.body:
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "upgrade"
        ):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, (ast.Assign, ast.AnnAssign))
                and isinstance(child.value, ast.Constant)
                and isinstance(child.value.value, str)
            ):
                targets = (
                    child.targets if isinstance(child, ast.Assign) else [child.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = child.value.value
    return values


def violations(source: str) -> list[str]:
    """Return line-specific unsafe upgrade operations, unless explicitly marked."""

    if any(CONTRACT_MARKER_RE.fullmatch(line) for line in source.splitlines()):
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno or 1}: migration is not valid Python: {exc.msg}"]

    found: list[str] = []
    assigned_strings = _upgrade_string_assignments(tree)
    for call in _upgrade_calls(tree):
        message: str | None = None
        if _is_op_call(call, "drop_table"):
            message = "op.drop_table removes a table"
        elif _is_op_call(call, "drop_column"):
            message = "op.drop_column removes a column"
        elif _is_op_call(call, "rename_table"):
            message = "op.rename_table renames a table"
        elif _is_op_call(call, "alter_column"):
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            if "new_column_name" in keywords:
                message = "op.alter_column renames a column"
            elif (
                isinstance(keywords.get("nullable"), ast.Constant)
                and keywords["nullable"].value is False
            ):
                message = "op.alter_column tightens a column to NOT NULL"
        elif _is_op_call(call, "execute"):
            values = _string_values(call)
            values.extend(
                assigned_strings[node.id]
                for node in ast.walk(call)
                if isinstance(node, ast.Name) and node.id in assigned_strings
            )
            for value in values:
                match = RAW_SQL_RE.search(value)
                if match:
                    message = f"op.execute contains destructive SQL ({match.group(0)})"
                    break

        if message:
            found.append(f"line {call.lineno}: {message}")
    return found


def changed_migrations(root: Path, base_ref: str, head_ref: str) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            f"{base_ref}...{head_ref}",
            "--",
            "alembic/versions/*.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [root / name for name in result.stdout.splitlines() if name]


def check_paths(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{path}: cannot read migration: {exc}")
            continue
        errors.extend(f"{path}: {problem}" for problem in violations(source))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref", required=True, help="base commit/ref for the PR diff"
    )
    parser.add_argument(
        "--head-ref", default="HEAD", help="head commit/ref (default: HEAD)"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        paths = changed_migrations(args.root, args.base_ref, args.head_ref)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or str(exc)
        print(
            f"migration expand-contract lint: cannot read git diff: {detail}",
            file=sys.stderr,
        )
        return 2

    errors = check_paths(paths)
    if errors:
        print("migration expand-contract lint failed:")
        print("\n".join(errors))
        print(
            "Add the documented contract-step marker only for an approved contract migration."
        )
        return 1
    print(f"migration expand-contract lint passed ({len(paths)} changed migration(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

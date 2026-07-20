"""ROB-981 (ROB-974 R2 H6-A) CP7 -- import purity and false-green-construct
guard across every rob974_h6a_* research-side module.

Static AST scan (no execution) -- mirrors ``test_rob974_import_guard.py``'s
discipline for H1, extended to forbid the same runtime/dynamic-authority
surfaces PLUS the specific false-green constructs the ROB-981 packet calls
out (dynamic ``__import__``/``importlib``, ``or True``, empty loop bodies,
TODO/pass-only stub functions in production code)."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = (
    "rob974_h6a_identity.py",
    "rob974_h6a_payload.py",
    "rob974_h6a_evidence.py",
    "rob974_h6a_accounting.py",
    "rob974_h6a_diagnostics.py",
    "rob974_h6a_smoke.py",
    "rob974_h6a_h2h3_adapter.py",
)
_FORBIDDEN = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "redis",
    "taskiq",
    "celery",
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "socket",
    "websockets",
    "boto3",
    "fastapi",
    "uvicorn",
    "random",
    "time",
    "datetime",
    "importlib",
    "subprocess",
    "os",
}


def test_h6a_modules_have_no_runtime_or_dynamic_authority_imports():
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] not in _FORBIDDEN for name in names), (
                module,
                names,
            )


def test_h6a_modules_have_no_dynamic_import_or_environment_authority():
    for module in _MODULES:
        text = (_ROOT / module).read_text()
        tree = ast.parse(text)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not names & {"__import__", "eval", "exec", "getenv"}, module
        assert "os.environ" not in text, module


def test_h6a_modules_have_no_current_time_or_random_calls():
    forbidden_attrs = {
        ("time", "time"),
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("random", "random"),
    }
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in forbidden_attrs, (
                    module,
                    node.attr,
                )


def test_h6a_modules_have_no_or_true_bypass():
    """`or True` is a classic false-green bypass -- a fail-closed `if not X
    or True:` (or similarly `condition or True`) would silently defeat the
    check it appears to guard."""
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                for value in node.values:
                    assert not (
                        isinstance(value, ast.Constant) and value.value is True
                    ), (
                        module,
                        "found an 'or True' bypass",
                    )


def test_h6a_modules_have_no_empty_loop_bodies():
    """A `for`/`while` loop whose body is only `pass` silently skips
    whatever accumulation/validation the surrounding code expects it to
    perform -- a common false-green shape."""
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.For | ast.While):
                assert not (
                    len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                ), (module, "found an empty (pass-only) loop body")


def test_h6a_modules_have_no_todo_or_stub_only_functions():
    """A production function whose ENTIRE body is a bare `pass`/`...`
    (Ellipsis)/docstring-then-pass is an unfinished stub masquerading as
    implemented -- ``__post_init__`` overrides with real validation logic
    are exempt by construction (this scan only flags a body with NOTHING
    but a docstring followed by pass/Ellipsis, or pass/Ellipsis alone)."""
    for module in _MODULES:
        text = (_ROOT / module).read_text()
        assert "TODO" not in text and "FIXME" not in text, module
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]  # skip a leading docstring
            if not body:
                continue
            only_stub = all(
                isinstance(stmt, ast.Pass)
                or (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is Ellipsis
                )
                for stmt in body
            )
            assert not only_stub, (module, node.name, "stub-only function body")

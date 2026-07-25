"""ROB-981 (ROB-974 R2 H6-A) CP7 -- static proof that
``app/services/rob974_h6a_bridge.py`` never begins/commits/rolls back/
closes a session or resolves a DB target itself.

Complements the pure-spy runtime coverage in
``test_rob974_h6a_bridge.py`` (``_PoisonedSession``) with a STATIC AST scan
that can never be defeated merely by a test forgetting to exercise some
code path -- it inspects the actual source for the forbidden call/attribute
shapes directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[3] / "app" / "services" / "rob974_h6a_bridge.py"
)

_FORBIDDEN_SESSION_METHODS = {
    "begin",
    "begin_nested",
    "commit",
    "rollback",
    "close",
    "sessionmaker",
}


def _tree() -> ast.AST:
    return ast.parse(_PATH.read_text())


def test_module_exists():
    assert _PATH.exists()


def test_no_direct_transaction_owning_calls():
    tree = _tree()
    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SESSION_METHODS:
            offending.append(node.attr)
    assert not offending, f"forbidden transaction-owning attribute access: {offending}"


def test_no_getenv_or_environ_authority():
    text = _PATH.read_text()
    assert "os.environ" not in text
    assert "getenv" not in text
    tree = _tree()
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"__import__", "eval", "exec"}


def test_no_current_time_or_random_calls():
    forbidden_attrs = {("time", "time"), ("datetime", "now"), ("random", "random")}
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in forbidden_attrs

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert "random" not in imported_modules


def test_no_or_true_bypass():
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                assert not (isinstance(value, ast.Constant) and value.value is True), (
                    "found an 'or True' bypass"
                )


def test_no_todo_or_fixme():
    text = _PATH.read_text()
    assert "TODO" not in text
    assert "FIXME" not in text

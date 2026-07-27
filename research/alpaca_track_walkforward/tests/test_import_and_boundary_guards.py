"""ROB-1062 H4 (AC26, AC31) — structural guards over the whole
``research/alpaca_track_walkforward/`` surface (recursive, excluding
``tests/``): no app/DB/broker/scheduler/random/time/alpaca import, no
wall-clock call, no broker mutation/scheduler token referenced lexically.

Closes a gap H1/H2/H3 all carried forward (their own closing reports flag
it explicitly, most recently H3's: "AST 가드가 importlib/__import__/os.times
3종 회피를 놓친다") in THIS package's own tree — H1/H2/H3 cannot be modified
(out of scope), so this is the one place that gap can actually be closed for
new code.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDED_DIR_NAMES = {"tests", "__pycache__"}


def _iter_source_modules(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in _EXCLUDED_DIR_NAMES for part in rel.parts[:-1]):
            continue
        if rel.name == "conftest.py":
            continue
        out.append(path)
    return out


_MODULES = [str(p.relative_to(_ROOT)) for p in _iter_source_modules(_ROOT)]

_FORBIDDEN_IMPORT_ROOTS = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "alembic",
    "redis",
    "taskiq",
    "celery",
    "prefect",
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib",
    "socket",
    "websockets",
    "boto3",
    "fastapi",
    "uvicorn",
    "random",
    "time",
    "alpaca",
    "alpaca_trade_api",
    # ROB-1062's own gap-closure: H1/H2/H3's guards only ban `import time` /
    # `import random` by root name -- `importlib.import_module("time")`,
    # `__import__("time")`, and `os.times()` (a real stdlib wall-clock-
    # adjacent call, easily confused with the banned `time` module by a
    # naive substring scan) all slipped past every prior phase's guard.
    "importlib",
}

_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}
_FORBIDDEN_OS_ATTRS = {
    "times"
}  # os.times() -- wall-clock-adjacent, HR-1062 gap closure
_FORBIDDEN_TOKENS = (
    "submit_order",
    "place_order",
    "cancel_order",
    "TaskiqScheduler",
    "@broker.task",
    "prefect.flow",
    "__import__",
)


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _forbidden_import_violations(mod: str, tree: ast.AST) -> list[tuple[str, str]]:
    violations = []
    for name in _imports(tree):
        root = name.split(".")[0]
        if root in _FORBIDDEN_IMPORT_ROOTS:
            violations.append((mod, name))
    return violations


def _wall_clock_hit(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_NOW_ATTRS:
                return True
    return False


def _os_times_hit(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FORBIDDEN_OS_ATTRS
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                return True
    return False


def _forbidden_token_violations(mod: str, text: str) -> list[tuple[str, str]]:
    violations = []
    for token in _FORBIDDEN_TOKENS:
        if token in text:
            violations.append((mod, token))
    return violations


def test_no_forbidden_imports_anywhere_in_h4():
    violations = []
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        violations.extend(_forbidden_import_violations(mod, tree))
    assert violations == [], f"forbidden imports found: {violations}"


def test_no_wall_clock_now_calls_anywhere_in_h4():
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        assert not _wall_clock_hit(tree), (
            f"{mod}: forbidden wall-clock .now()-style call"
        )


def test_no_os_times_call_anywhere_in_h4():
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        assert not _os_times_hit(tree), f"{mod}: forbidden os.times() call"


def test_no_broker_order_fill_or_scheduler_symbols_referenced():
    violations = []
    for mod in _MODULES:
        text = (_ROOT / mod).read_text()
        violations.extend(_forbidden_token_violations(mod, text))
    assert violations == [], (
        f"forbidden broker/scheduler/dunder-import tokens: {violations}"
    )


def test_forbidden_import_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text("import sqlalchemy\n")
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _forbidden_import_violations("bad.py", tree) == [("bad.py", "sqlalchemy")]


def test_forbidden_import_scanner_catches_importlib_the_h1_h3_gap():
    text = "import importlib\nimportlib.import_module('time')\n"
    tree = ast.parse(text)
    assert _forbidden_import_violations("bad.py", tree) == [("bad.py", "importlib")]


def test_wall_clock_scanner_catches_a_synthetic_violation():
    tree = ast.parse("import datetime\ndatetime.datetime.now()\n")
    assert _wall_clock_hit(tree) is True


def test_os_times_scanner_catches_a_synthetic_violation():
    tree = ast.parse("import os\nos.times()\n")
    assert _os_times_hit(tree) is True


def test_os_times_scanner_does_not_false_positive_on_unrelated_dot_times():
    tree = ast.parse("class X:\n    def times(self):\n        pass\nX().times()\n")
    assert _os_times_hit(tree) is False


def test_forbidden_token_scanner_catches_dunder_import():
    text = "def f():\n    return __import__('time')\n"
    assert _forbidden_token_violations("bad.py", text) == [("bad.py", "__import__")]


def test_all_h4_source_modules_are_accounted_for():
    """Drift guard: a new module added anywhere under
    research/alpaca_track_walkforward/ must be added here."""
    expected = {
        "blind_counts.py",
        "config_selection.py",
        "context_binding.py",
        "fill_model.py",
        "fold_schedule.py",
        "oos_mask.py",
        "pnl_views.py",
        "provider_evidence.py",
        "run_manifest.py",
        "runner.py",
        "trade_ledger.py",
        "wf_seal_consumption.py",
    }
    discovered = {str(p.relative_to(_ROOT)) for p in _iter_source_modules(_ROOT)}
    assert discovered == expected, (
        f"module list drift: discovered {sorted(discovered)}, expected {sorted(expected)}"
    )

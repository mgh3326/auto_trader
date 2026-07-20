"""ROB-983 (H5) -- AST/import boundary guard for all rob974_h5_* modules.

Mirrors ``test_rob974_h2_import_guard.py``'s pattern: static
``ast.Import``/``ast.ImportFrom`` scan against a forbidden-prefix list and an
explicit allowlist, plus a ``ast.Call`` walk rejecting dynamic-import calls
(``__import__``/``importlib.import_module``) and current-time calls
(``.now()``/``.today()``/``.utcnow()``) -- H5 is pure: no DB/network/broker/
scheduler/random/current-time/physical-file-IO/validated-gate import or call
anywhere in its production surface.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _rob974_h5_modules() -> list[str]:
    return sorted(p.name for p in _ROOT.glob("rob974_h5_*.py"))


_FORBIDDEN_PREFIXES = (
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "alembic",
    "redis",
    "taskiq",
    "celery",
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
    "os",
    "subprocess",
    "pathlib",
)

# Explicit allowlist. Additive per checkpoint -- CP1-CP7 each append their own
# new rob974_h5_* siblings and the stdlib primitives they compose (never
# app/DB/network/broker/order/fill/scheduler/random/current-time/
# physical-file-IO/validated-gate).
_ALLOWED = {
    "__future__",
    "collections",
    "collections.abc",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "math",
    "re",
    "typing",
    "rob974_h5_canonical",
    "rob974_h5_contracts",
    "rob974_h5_dual_evidence",
    "rob974_h5_gates",
    "rob974_h5_markdown",
    "rob974_h5_s3",
    "rob974_h5_s4",
}

_CURRENT_TIME_ATTRS = ("now", "today", "utcnow")


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _is_dynamic_import_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "__import__":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _is_current_time_call(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr in _CURRENT_TIME_ATTRS


def _forbidden_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_dynamic_import_call(node):
            violations.append(f"dynamic import call at line {node.lineno}")
        if _is_current_time_call(node):
            violations.append(f"current-time call at line {node.lineno}")
    return violations


def test_no_forbidden_imports():
    modules = _rob974_h5_modules()
    assert modules, "expected at least one rob974_h5_*.py module to exist"
    for mod in modules:
        for name in _imports(_ROOT / mod):
            top = name.split(".")[0]
            assert top not in _FORBIDDEN_PREFIXES, (
                f"{mod} imports forbidden module {name!r}"
            )


def test_only_allowlisted_modules_are_imported():
    for mod in _rob974_h5_modules():
        for name in _imports(_ROOT / mod):
            assert name in _ALLOWED, f"{mod} imports non-allowlisted module {name!r}"


def test_no_validated_gate_import():
    for mod in _rob974_h5_modules():
        for name in _imports(_ROOT / mod):
            assert "validated_gate" not in name, (
                f"{mod} imports the validated-gate module {name!r} (forbidden)"
            )


def test_no_scorecard_writer_import():
    for mod in _rob974_h5_modules():
        for name in _imports(_ROOT / mod):
            assert "rob960_scorecard_writer" not in name, (
                f"{mod} imports the physical scorecard writer {name!r} (forbidden)"
            )


def test_no_dynamic_import_or_current_time_calls():
    for mod in _rob974_h5_modules():
        violations = _forbidden_calls(_ROOT / mod)
        assert violations == [], f"{mod} has forbidden call(s): {violations}"


class TestGuardCatchesInjectedBypasses:
    """Regression proof: inject each bypass into a SCRATCH file (never a
    production module) and confirm the scan actually flags it."""

    def test_dynamic_import_call_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dynamic_socket.py"
            path.write_text("mod = __import__('socket')\n")
            assert _forbidden_calls(path) != []

    def test_importlib_import_module_call_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "importlib_socket.py"
            path.write_text(
                "import importlib\nmod = importlib.import_module('socket')\n"
            )
            assert _forbidden_calls(path) != []

    def test_datetime_now_call_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "datetime_now.py"
            path.write_text("from datetime import datetime\nx = datetime.now()\n")
            assert _forbidden_calls(path) != []

    def test_datetime_utcnow_and_today_are_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "datetime_utcnow.py"
            path.write_text(
                "from datetime import datetime\n"
                "x = datetime.utcnow()\n"
                "y = datetime.today()\n"
            )
            violations = _forbidden_calls(path)
            assert len(violations) == 2

    def test_forbidden_prefix_import_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db_import.py"
            path.write_text("import sqlalchemy\n")
            imports = list(_imports(path))
            assert any(name.split(".")[0] in _FORBIDDEN_PREFIXES for name in imports)

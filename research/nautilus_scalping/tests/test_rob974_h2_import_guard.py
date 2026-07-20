"""ROB-979 (H2, ROB-974 R2) -- AST/import boundary guard for all rob974_h2_* modules.

Mirrors ``test_rob940_import_guard.py``'s pattern. AC38 (H2 doc): AST/import
guards prohibit app/DB/network/broker/order/fill/scheduler/random/
current-time/validated-gate imports across every rob974_h2_* module.

verify-R1 finding 5 hardening: the original guard only inspected static
``ast.Import``/``ast.ImportFrom`` nodes, so ``__import__('socket')`` (not an
Import node at all) and ``datetime.now()``/``.today()``/``.utcnow()``
(``datetime`` is a legitimately allowlisted STATIC import, used for
deterministic ``datetime.fromtimestamp(ts, tz=UTC)`` UTC-date bucketing --
the guard never inspected what CALLS were made through it) both silently
passed. This module now additionally walks every ``ast.Call`` node to reject
dynamic-import calls (``__import__(...)``, ``importlib.import_module(...)``)
and current-time calls (``.now()``/``.today()``/``.utcnow()``), independent
of whether the underlying module name is itself allowlisted.
"""

import ast
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _rob974_h2_modules() -> list[str]:
    return sorted(p.name for p in _ROOT.glob("rob974_h2_*.py"))


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
)

# Explicit allowlist. Additive per checkpoint -- CP2/CP3/CP4 append their own
# new rob974_h2_* siblings and the reusable frozen research primitives they
# compose (never app/DB/network/broker/order/fill/scheduler/random/
# current-time/validated-gate).
_ALLOWED = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "math",
    "typing",
    "research_contracts.canonical_hash",
    "rob940_cost_model",
    "rob941_funding_sidecar",
    "rob941_gaps",
    "rob974_features",
    "rob974_h2_dtos",
    "rob974_h2_h1_bridge",
    "rob974_h2_ingress",
    "rob974_h2_s3_engine",
    "rob974_h2_s4_engine",
    "rob974_h2_scenarios",
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
    modules = _rob974_h2_modules()
    assert modules, "expected at least one rob974_h2_*.py module to exist"
    for mod in modules:
        for name in _imports(_ROOT / mod):
            top = name.split(".")[0]
            assert top not in _FORBIDDEN_PREFIXES, (
                f"{mod} imports forbidden module {name!r}"
            )


def test_only_allowlisted_modules_are_imported():
    for mod in _rob974_h2_modules():
        for name in _imports(_ROOT / mod):
            assert name in _ALLOWED, f"{mod} imports non-allowlisted module {name!r}"


def test_no_validated_gate_import():
    for mod in _rob974_h2_modules():
        for name in _imports(_ROOT / mod):
            assert "validated_gate" not in name, (
                f"{mod} imports the validated-gate module {name!r} (forbidden)"
            )


def test_no_dynamic_import_or_current_time_calls():
    """verify-R1 finding 5: hardened Call-level scan on the ACTUAL production
    modules -- catches what the static-Import-only scan above cannot."""
    for mod in _rob974_h2_modules():
        violations = _forbidden_calls(_ROOT / mod)
        assert violations == [], f"{mod} has forbidden call(s): {violations}"


class TestGuardCatchesInjectedBypasses:
    """Regression proof (mirrors the verifier's own repro): inject each
    bypass into a SCRATCH file (never a production module) and confirm the
    hardened scan actually flags it -- proving the new checks are not
    themselves vacuous."""

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

    def test_deterministic_fromtimestamp_call_is_not_flagged(self):
        # the real production usage (rob974_h2_s3_engine._utc_date) must
        # remain unaffected -- it is deterministic given its ts_ms input,
        # never current-time.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deterministic_fromtimestamp.py"
            path.write_text(
                "from datetime import UTC, datetime\n"
                "x = datetime.fromtimestamp(0 / 1000.0, tz=UTC).date()\n"
            )
            assert _forbidden_calls(path) == []

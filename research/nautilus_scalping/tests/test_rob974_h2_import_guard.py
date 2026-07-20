"""ROB-979 (H2, ROB-974 R2) -- AST/import boundary guard for all rob974_h2_* modules.

Mirrors ``test_rob940_import_guard.py``'s pattern. AC38 (H2 doc): AST/import
guards prohibit app/DB/network/broker/order/fill/scheduler/random/
current-time/validated-gate imports across every rob974_h2_* module.
"""

import ast
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
    "rob974_h2_dtos",
    "rob974_h2_ingress",
    "rob974_h2_s3_engine",
    "rob974_h2_s4_engine",
    "rob974_h2_scenarios",
}


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


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

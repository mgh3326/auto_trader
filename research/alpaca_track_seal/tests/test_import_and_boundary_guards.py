"""ROB-1060 H2 — structural guards for the PURE seal modules.

Mirrors ``research/alpaca_track/tests/test_import_and_time_guards.py`` (H1)
and ``research/nautilus_scalping/tests/test_rob944_import_guard.py``: every
pure module in this list must never import app/DB/network/broker/scheduler
surfaces, ANYWHERE in the file (module scope or nested) — these are data/
identity builders, never the registration boundary.

``registry_cli.py`` is DELIBERATELY EXCLUDED from this scan (its whole job is
to bridge to ``app.services.strategy_experiment_registry``) — it has its own,
separate module-scope-only guard in ``test_registry_cli_import_guard.py``,
exactly mirroring ``research/nautilus_scalping/tests/
test_rob944_cli_import_guard.py``'s split between a pure-module guard and a
CLI module-scope-only guard.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_PURE_MODULES = [
    "artifact.py",
    "configs.py",
    "identity.py",
    "params.py",
    "source_provenance.py",
]

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
}

_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}
_FORBIDDEN_TOKENS = (
    "submit_order",
    "place_order",
    "cancel_order",
    "TaskiqScheduler",
    "@broker.task",
    "prefect.flow",
)


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_forbidden_imports_anywhere_in_pure_modules():
    for mod in _PURE_MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        for name in _imports(tree):
            root = name.split(".")[0]
            assert root not in _FORBIDDEN_IMPORT_ROOTS, (
                f"{mod} imports forbidden module {name!r} (root {root!r}) — "
                "pure seal modules never touch app/DB/network/broker/scheduler"
            )
            assert "alpaca" not in name.lower(), (
                f"{mod} imports an Alpaca-named module {name!r}"
            )


def test_no_wall_clock_now_calls_in_pure_modules():
    for mod in _PURE_MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_NOW_ATTRS:
                raise AssertionError(
                    f"{mod}: forbidden wall-clock call `.{func.attr}(...)`"
                )


def test_no_broker_order_fill_or_scheduler_tokens_referenced():
    for mod in _PURE_MODULES:
        text = (_ROOT / mod).read_text()
        for token in _FORBIDDEN_TOKENS:
            assert token not in text, f"{mod} references forbidden token {token!r}"


def test_pure_module_list_is_exhaustive_over_the_package_root():
    """Drift guard: every .py file directly under the package root (not
    tests/, not sealed_source_data/, not registry_cli.py, not conftest.py)
    must be accounted for in ``_PURE_MODULES``."""
    excluded_names = {"conftest.py", "registry_cli.py", "__init__.py"}
    discovered = {p.name for p in _ROOT.glob("*.py") if p.name not in excluded_names}
    assert discovered == set(_PURE_MODULES), (
        f"module list drift: discovered {sorted(discovered)}, expected "
        f"{sorted(_PURE_MODULES)}"
    )


def test_registry_cli_exists_and_is_the_only_module_excluded_from_the_pure_scan():
    assert (_ROOT / "registry_cli.py").is_file(), (
        "registry_cli.py must exist — see test_registry_cli_import_guard.py "
        "for its own, separate module-scope-only guard"
    )

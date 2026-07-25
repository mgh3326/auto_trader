"""ROB-942 (H2, ROB-940) — AC1 import-boundary guard.

Mirrors the existing ``test_pit_data_layer_guard.py`` AST-check pattern, but
covers the full AC1 forbidden surface for the engine modules specifically:
DB/network/app-settings/broker/order/fill/scheduler. A module either imports
only stdlib + the allowed research-local names, or this test fails.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = ["rob940_bars_agg.py", "rob940_cost_model.py", "rob940_engine.py"]

# Forbidden top-level import prefixes: app settings/broker/order/fill/scheduler
# surfaces, plus common DB/network client libraries. Anything matching one of
# these (as an exact name or dotted-prefix) fails the guard.
_FORBIDDEN_PREFIXES = (
    "app",  # app.* settings/broker/order/fill/scheduler/db surfaces
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
)

# Explicit allowlist for what these pure engine modules MAY import.
_ALLOWED = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "math",
    "typing",
    "research_contracts.canonical_hash",
    "rob940_bars_agg",
    "rob940_cost_model",
}


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_forbidden_db_network_app_broker_order_fill_scheduler_imports():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            top = name.split(".")[0]
            assert top not in _FORBIDDEN_PREFIXES, (
                f"{mod} imports forbidden module {name!r} (AC1: no DB/network/"
                "app-settings/broker/order/fill/scheduler imports)"
            )


def test_only_allowlisted_modules_are_imported():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            assert name in _ALLOWED, f"{mod} imports non-allowlisted module {name!r}"

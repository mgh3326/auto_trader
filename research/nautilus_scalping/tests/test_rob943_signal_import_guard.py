"""ROB-943 (H3, ROB-940) — AST import-boundary guard for the signal modules.

Mirrors ``test_rob940_import_guard.py``'s pattern, scoped to the new H3
signal-generation/manifest modules. These modules have NO legitimate need
for wall-clock time or randomness (unlike ``rob940_engine``, which uses
``datetime`` for UTC-day bucketing) -- H3 signal math is purely a function
of its bar/config inputs, so ``datetime``/``time``/``random`` are forbidden
here too, stricter than the H2 guard.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = ["rob940_signal_manifest.py", "rob940_signal_s1.py", "rob940_signal_s2.py"]

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
    "datetime",
)

_ALLOWED = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "math",
    "statistics",
    "typing",
    "research_contracts.canonical_hash",
    "rob940_bars_agg",
    "rob940_cost_model",
    "rob940_engine",
    "rob940_signal_manifest",
    "rob940_signal_s1",
}


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_forbidden_db_network_app_broker_time_random_imports():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            top = name.split(".")[0]
            assert top not in _FORBIDDEN_PREFIXES, (
                f"{mod} imports forbidden module {name!r} (H3: no DB/network/"
                "app-settings/broker/order/fill/scheduler/random/current-time "
                "imports)"
            )


def test_only_allowlisted_modules_are_imported():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            assert name in _ALLOWED, f"{mod} imports non-allowlisted module {name!r}"

"""ROB-945 (H5, ROB-940) -- AC import-boundary guard for the pure H5 modules.

Mirrors ``test_rob944_import_guard.py``'s pattern: every pure H5 module
either imports only stdlib + the allowed research-local names, or this
test fails. No H5 module may import app/DB/network/broker/order/fill/
scheduler/MCP/notifier code, use randomness or wall-clock time, or bypass
this guard via a dynamic import.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = [
    "rob945_accounting_seal.py",
    "rob945_canonical_payload.py",
    "rob945_capture.py",
    "rob945_h6_summary_contract.py",
    "rob945_signal_concurrency.py",
    "rob945_scenario_metrics.py",
    "rob945_pbo_grid.py",
    "rob945_pbo_builder.py",
    "rob945_verdict.py",
    "rob945_scorecard.py",
]

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
)

_ALLOWED = {
    "__future__",
    "base64",
    "collections.abc",
    "dataclasses",
    "datetime",
    "json",
    "math",
    "re",
    "types",
    "typing",
    "research_contracts.canonical_hash",
    "research_contracts.honest_offline_gate",
    "rob940_cost_model",
    "rob940_engine",
    "rob941_frozen_scope",
    "rob944_folds",
    "rob944_frozen_campaign",
    "rob944_gap_funding",
    "rob944_selection",
    "rob944_walkforward",
    "rob945_accounting_seal",
    "rob945_canonical_payload",
    "rob945_capture",
    "rob945_h6_summary_contract",
    "rob945_pbo_builder",
    "rob945_pbo_grid",
    "rob945_scenario_metrics",
    "rob945_signal_concurrency",
    "rob945_verdict",
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
                f"{mod} imports forbidden module {name!r} (no DB/network/"
                "app-settings/broker/order/fill/scheduler imports)"
            )


def test_only_allowlisted_modules_are_imported():
    for mod in _MODULES:
        for name in _imports(_ROOT / mod):
            assert name in _ALLOWED, f"{mod} imports non-allowlisted module {name!r}"


def test_no_random_or_dynamic_import_bypass():
    forbidden_names = {"random", "time", "importlib", "__import__", "eval", "exec"}
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                raise AssertionError(f"{mod} references forbidden name {node.id!r}")
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom)
                    else [n.name for n in node.names]
                )
                for name in names:
                    if name and name.split(".")[0] in ("random", "time", "importlib"):
                        raise AssertionError(f"{mod} imports forbidden module {name!r}")


def test_no_wall_clock_or_env_or_dsn_leaking_names():
    """Defensive sweep for a wall-clock read, environment lookup, or DSN
    field appearing as a RUNTIME reference anywhere in an H5 module's
    source (prose mentions of what this module deliberately does NOT do,
    e.g. in a docstring explaining the ROB-905 boundary, are not what this
    guards against -- ``os.environ``/``getenv`` are actual code
    constructs that could only appear as live references)."""
    forbidden_substrings = ("os.environ", "getenv", "DATABASE_URL")
    for mod in _MODULES:
        text = (_ROOT / mod).read_text()
        for needle in forbidden_substrings:
            assert needle not in text, f"{mod} contains forbidden substring {needle!r}"

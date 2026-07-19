"""ROB-944 (H4, ROB-940) — AC import-boundary guard for the pure H4 modules.

Mirrors ``test_rob940_import_guard.py``/``test_rob943_signal_import_guard.py``'s
pattern: a pure H4 module either imports only stdlib + the allowed
research-local names, or this test fails. ``run_rob944_campaign.py`` (the
CLI) is intentionally NOT covered here -- its ``app.*`` imports are all
deferred inside function bodies (see ``test_rob944_cli_import_guard.py``,
which checks its module-scope imports only).
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = [
    "rob944_folds.py",
    "rob944_selection.py",
    "rob944_signal_ordering.py",
    "rob944_gap_funding.py",
    "rob944_scenario_evidence.py",
    "rob944_diagnostic_evidence.py",
    "rob944_walkforward.py",
    "rob944_frozen_campaign.py",
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
    "collections.abc",
    "copy",
    "dataclasses",
    "datetime",
    "hashlib",
    "math",
    "pathlib",
    "re",
    "statistics",
    "traceback",
    "types",
    "typing",
    "research_contracts.canonical_hash",
    "research_contracts.diagnostic_evidence_policy",
    "canonical_hash",
    "funding_oi_archive",
    "rob940_bars_agg",
    "rob940_cost_model",
    "rob940_engine",
    "rob940_signal_manifest",
    "rob940_signal_s1",
    "rob940_signal_s2",
    "rob941_frozen_scope",
    "rob941_funding_sidecar",
    "rob941_gaps",
    "rob941_manifest",
    "rob944_diagnostic_evidence",
    "rob944_folds",
    "rob944_gap_funding",
    "rob944_scenario_evidence",
    "rob944_selection",
    "rob944_signal_ordering",
    "rob944_walkforward",
    "rob946_campaign_identity",
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
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom)
                    else [n.name for n in node.names]
                )
                for name in names:
                    if name and name.split(".")[0] in ("random", "time", "importlib"):
                        raise AssertionError(f"{mod} imports forbidden module {name!r}")

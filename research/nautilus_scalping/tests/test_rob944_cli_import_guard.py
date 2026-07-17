"""ROB-944 (H4, ROB-940) — CLI module-scope import guard.

``run_rob944_campaign.py`` is a CLI, not a pure module -- its ``run`` mode
legitimately needs ``app.*`` (DB session, registry bridge) to do real work.
But ``--help``/``--version``/``plan`` must NEVER touch DB/network/broker
surfaces, so every ``app.*``/DB/network import must be DEFERRED inside a
function body, never at module scope. This guard inspects ONLY the
top-level (module-scope) import statements -- deferred imports inside
``def``/``async def`` bodies are expected and allowed.
"""

import ast
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "run_rob944_campaign.py"

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
    "socket",
    "websockets",
    "boto3",
    "fastapi",
    "uvicorn",
)


def _module_scope_import_names(tree: ast.Module):
    for node in tree.body:  # top-level only -- do NOT recurse into functions
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_module_scope_imports_never_touch_db_network_broker():
    tree = ast.parse(_SCRIPT.read_text())
    for name in _module_scope_import_names(tree):
        top = name.split(".")[0]
        assert top not in _FORBIDDEN_PREFIXES, (
            f"run_rob944_campaign.py imports {name!r} at MODULE SCOPE -- "
            "--help/--version/plan must never touch DB/network/broker; move "
            "this import inside the function that actually needs it"
        )


def test_app_imports_exist_but_only_inside_function_bodies():
    """Sanity check the guard above isn't vacuous -- the script DOES use
    app.* somewhere (deferred), proving the guard is exercising a real
    boundary, not an absent one."""
    tree = ast.parse(_SCRIPT.read_text())
    found_deferred_app_import = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.ImportFrom)
                    and inner.module
                    and inner.module.startswith("app.")
                ):
                    found_deferred_app_import = True
    assert found_deferred_app_import, "expected at least one deferred app.* import"

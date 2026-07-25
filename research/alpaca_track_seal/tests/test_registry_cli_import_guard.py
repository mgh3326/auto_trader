"""ROB-1060 H2 — CLI module-scope import guard.

Mirrors ``research/nautilus_scalping/tests/test_rob944_cli_import_guard.py``
exactly: ``registry_cli.py`` is a CLI, not a pure module — its ``register``
mode legitimately needs ``app.*`` (DB session, registry bridge). But ``plan``
must NEVER touch DB/network/broker surfaces, so every ``app.*``/DB import
must be DEFERRED inside a function body, never at module scope. This guard
inspects ONLY the top-level (module-scope) import statements.
"""

import ast
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "registry_cli.py"

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
            f"registry_cli.py imports {name!r} at MODULE SCOPE -- plan must "
            "never touch DB/network/broker; move this import inside the "
            "function that actually needs it"
        )


def test_app_imports_exist_but_only_inside_function_bodies():
    """Sanity check the guard above isn't vacuous -- the script DOES use
    app.* somewhere (deferred), proving the guard exercises a real boundary."""
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


def test_plan_subcommand_runs_with_zero_db_network_and_prints_semantic_hash():
    """End-to-end proof (not just AST inspection): a genuinely separate
    subprocess invocation of ``plan`` succeeds with no DATABASE_URL, no
    network, and no ALPACA_TRACK_SEAL_REGISTER_WRITE_OPT_IN set."""
    package_root = _SCRIPT.parent
    research_root = package_root.parent
    repo_root = research_root.parent
    nautilus_scalping = research_root / "nautilus_scalping"
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ":".join(
            str(p) for p in (package_root, nautilus_scalping, repo_root)
        ),
    }
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "plan"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    import json

    payload = json.loads(result.stdout)
    assert len(payload["semantic_hash"]) == 64
    assert payload["config_count"] == 16
    assert len(payload["specs"]) == 16


def test_register_subcommand_refuses_without_env_opt_in():
    """``register`` must fail closed (never silently register) when the
    default-off write opt-in env var is absent -- proven by a real subprocess
    invocation, not a mock."""
    package_root = _SCRIPT.parent
    research_root = package_root.parent
    repo_root = research_root.parent
    nautilus_scalping = research_root / "nautilus_scalping"
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ":".join(
            str(p) for p in (package_root, nautilus_scalping, repo_root)
        ),
    }
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "register", "--confirm"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "not enabled" in result.stderr

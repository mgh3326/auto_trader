"""ROB-978 pure-H1 import and dynamic-authority guard."""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = ("rob974_features.py", "rob974_lineage.py", "rob974_smoke.py")
_FORBIDDEN = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "redis",
    "taskiq",
    "celery",
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "socket",
    "websockets",
    "boto3",
    "fastapi",
    "uvicorn",
    "random",
    "time",
    "datetime",
    "importlib",
}


def test_rob974_modules_have_no_runtime_or_dynamic_authority_imports():
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] not in _FORBIDDEN for name in names), module


def test_rob974_modules_have_no_dynamic_import_or_environment_authority():
    for module in _MODULES:
        tree = ast.parse((_ROOT / module).read_text())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not names & {"__import__", "eval", "exec", "getenv"}, module
        assert "os.environ" not in (_ROOT / module).read_text(), module

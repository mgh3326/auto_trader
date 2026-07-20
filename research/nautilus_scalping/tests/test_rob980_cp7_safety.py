from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import rob974_h3_smoke as smoke

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_ROOT = Path(__file__).resolve().parents[1]
_H3_CORE = (
    "rob974_h3_manifest.py",
    "rob974_h3_s3.py",
    "rob974_h3_s4.py",
    "rob974_h3_evidence.py",
)
_H3_ALL = (*_H3_CORE, "rob974_h3_smoke.py")
_FORBIDDEN_IMPORT_ROOTS = {
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
    "os",
}


def _tree(module: str) -> ast.Module:
    return ast.parse((_MODULE_ROOT / module).read_text())


def test_h3_modules_have_no_app_db_network_broker_clock_random_or_dynamic_authority():
    for module in _H3_ALL:
        tree = _tree(module)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            else:
                continue
            assert all(
                name.split(".")[0] not in _FORBIDDEN_IMPORT_ROOTS for name in imported
            ), module
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        source = (_MODULE_ROOT / module).read_text()
        assert not names & {"__import__", "eval", "exec", "getenv"}, module
        assert "os.environ" not in source, module
        assert "PYTHONPATH" not in source or module == "rob974_h3_smoke.py"


def test_h3_core_has_no_h2_executor_funding_horizon_or_forbidden_formula_dependency():
    for module in _H3_CORE:
        source = (_MODULE_ROOT / module).read_text().lower()
        tree = _tree(module)
        imports = {
            name
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }
        assert not any("h2" in name for name in imports), module
        assert "expm1" not in source, module
        assert "pair_executor" not in imports, module
        assert "funding" not in imports, module
        assert "horizon" not in imports, module


def test_smoke_has_no_exception_swallowing_or_empirical_execution_entrypoint():
    tree = _tree("rob974_h3_smoke.py")
    assert not any(isinstance(node, (ast.Try, ast.TryStar)) for node in ast.walk(tree))
    source = (_MODULE_ROOT / "rob974_h3_smoke.py").read_text()
    assert "--run" not in source
    assert "materializer" not in source.lower()
    assert "NOT_EXECUTED_AWAITING_ORCH_GO" in source
    assert "actual_h2_engine_integration" in source


def _probe(seed: str) -> str:
    code = (
        "import json; import rob974_h3_smoke as s; "
        "print(json.dumps(s.deterministic_hash_probe(),sort_keys=True,"
        "separators=(',',':')))"
    )
    completed = subprocess.run(
        (str(_REPO_ROOT / ".venv/bin/python"), "-c", code),
        cwd=_REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": str(_MODULE_ROOT),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert "rob-979" not in str(_MODULE_ROOT)
    return completed.stdout.strip()


def test_real_h1_h3_probe_is_identical_across_hash_seeds_and_pinned():
    expected = json.dumps(
        smoke.deterministic_hash_probe(), sort_keys=True, separators=(",", ":")
    )
    assert _probe("1") == expected
    assert _probe("987654321") == expected
    assert json.loads(expected) == {
        "S3": "cd0367dcd1b3d0723e7f7815144d2120dba80f0f2603fe1a88eb3f7b234df7ac",
        "S4": "778381ed58a245dce084a76a855966914afe9377264a75a82fcf70cccee0545c",
        "feature_hash": (
            "5abc827a3a3d28c02d4ad5313a299dcf0bbf95397c66d80d5e359d95193e19a2"
        ),
    }

"""Enforce: order_proposals repository is imported ONLY by its service module.

Model: tests/services/brokers/binance/demo/test_no_testnet_imports.py
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BANNED = "app.services.order_proposals.repository"
_ALLOWED = {pathlib.Path("app/services/order_proposals/service.py")}


def _is_banned(module: str | None) -> bool:
    if not module:
        return False
    return module == _BANNED or module.startswith(_BANNED + ".")


def _imports_repo(path: pathlib.Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_banned(node.module):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned(alias.name):
                    return True
    return False


@pytest.mark.unit
def test_repository_import_boundary_enforced():
    offenders = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel in _ALLOWED:
            continue
        if _imports_repo(path):
            offenders.append(str(rel))
    assert not offenders, f"repository imported outside its service: {offenders}"


@pytest.mark.unit
def test_service_actually_imports_repository():
    svc = REPO_ROOT / "app/services/order_proposals/service.py"
    assert _imports_repo(svc), (
        "service.py must import the repository (guard would be vacuous)"
    )

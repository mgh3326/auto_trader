"""Import / boundary guards for the KR backtest harness package."""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent

# Runtime app / broker surfaces that research harness must not import.
_FORBIDDEN_IMPORT_PREFIXES = (
    "app.",
    "app.models",
    "app.services",
    "app.mcp_server",
)


def _iter_py_files():
    for path in sorted(_PKG.rglob("*.py")):
        if path.name == "conftest.py":
            continue
        yield path


def test_no_app_or_broker_imports():
    offenders: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(
                        _FORBIDDEN_IMPORT_PREFIXES
                    ) or alias.name in {"app"}:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "app" or mod.startswith("app."):
                    offenders.append(f"{path.name}: from {mod}")
    assert offenders == []


def test_no_is_active_anywhere_in_package():
    for path in _iter_py_files():
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "is_active" not in text, path


def test_windows_reuse_closed_window():
    from windows import EXPLORATION_WINDOW, HOLDOUT_WINDOW

    from research_contracts.evaluation_windows import ClosedWindow

    assert isinstance(EXPLORATION_WINDOW, ClosedWindow)
    assert EXPLORATION_WINDOW.start == "2015-01-01"
    assert EXPLORATION_WINDOW.end == "2024-12-31"
    assert HOLDOUT_WINDOW.start == "2025-01-01"
    assert HOLDOUT_WINDOW.end == "2026-07-31"


def test_job_purpose_constant():

    # package __init__ loaded as flat module name is awkward; read file.
    text = (_PKG / "__init__.py").read_text(encoding="utf-8")
    assert "BACKTEST_HARNESS_WIRING_ONLY" in text
    assert "PIPELINE_SMOKE_NOT_A_STRATEGY" in text

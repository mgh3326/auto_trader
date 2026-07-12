"""ROB-843 safety/architecture guards.

* Automated KIS mock mutation stays default-off until ROB-853 lands.
* The executor final-risk path (executor + risk gate + durable ledger loader)
  imports no KIS-live execution/ledger module — mock stays isolated from live.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings

# KIS-live execution/ledger/policy fragments that mock scalping must never reach.
_FORBIDDEN_LIVE_FRAGMENTS = (
    "kis_live_ledger",
    "live_order_ledger",
    "live_order_evidence",
    "kis_live",
)

_MODULES_UNDER_GUARD = (
    "app/services/brokers/kis/mock_scalping_exec/executor.py",
    "app/services/brokers/kis/mock_scalping_exec/ledger_state.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


@pytest.mark.unit
def test_automated_kis_mock_mutation_defaults_off() -> None:
    s = Settings()
    assert s.kis_mock_scalping_ws_enabled is False
    assert s.kis_mock_scalping_ws_confirm is False
    assert s.WATCH_AUTO_EXECUTE_MOCK_ENABLED is False


@pytest.mark.unit
def test_executor_risk_path_imports_no_kis_live() -> None:
    violations: list[str] = []
    for rel in _MODULES_UNDER_GUARD:
        path = _REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            for fragment in _FORBIDDEN_LIVE_FRAGMENTS:
                if fragment in module:
                    violations.append(f"{rel}: imports {module!r} ({fragment!r})")
    assert not violations, "mock scalping reached KIS-live code:\n" + "\n".join(
        violations
    )

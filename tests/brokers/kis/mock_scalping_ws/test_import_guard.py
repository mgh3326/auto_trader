"""AST import guard: the read-only quote WS package must reach no order/ledger code.

ROB-321 PR2: ``app/services/brokers/kis/mock_scalping_ws/`` streams market data
only. It may use read-only infra (approval keys, WS constants/protocol, parsers)
but must never import any order-submission, order-validation, trading-service,
execution-mutation, or ledger/reconcile module — keeping the order safety
boundary structural, not merely conventional.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.services.brokers.kis.mock_scalping_ws as pkg

_PACKAGE_DIR = Path(pkg.__file__).parent

# Substrings that must not appear in any imported module path.
_FORBIDDEN_IMPORT_FRAGMENTS = (
    "order_execution",
    "order_validation",
    "orders_",
    ".orders",
    "kis_trading_service",
    "trading_service",
    "execution_client",
    "domestic_orders",
    "overseas_orders",
    "kis_mock_lifecycle",
    "kis_mock_holdings",
    "holdings_reconciler",
    "ledger",
    "reconcil",
    "brokers.binance",
)


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


@pytest.mark.unit
def test_quote_ws_package_imports_no_order_or_ledger_module() -> None:
    py_files = sorted(_PACKAGE_DIR.glob("*.py"))
    assert py_files, "expected at least one module in mock_scalping_ws"

    violations: list[str] = []
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            for fragment in _FORBIDDEN_IMPORT_FRAGMENTS:
                if fragment in module:
                    violations.append(
                        f"{path.name}: imports {module!r} (matched {fragment!r})"
                    )

    assert not violations, (
        "read-only quote WS package reached forbidden code:\n" + "\n".join(violations)
    )

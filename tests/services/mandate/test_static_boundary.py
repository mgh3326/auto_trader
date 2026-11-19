"""Acceptance 5: the mandate package imports no order or broker module.

Direct imports are scanned with ``ast`` and the ``app.services`` import closure is
walked statically. No database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "app/services/mandate"
FORBIDDEN = (
    "app.services.brokers",
    "app.mcp_server",
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.auto_approve",
    "app.services.order_proposals.service",
    "app.services.order_proposals.telegram_callback",
    "app.services.mechanical_loss_cut",
    "app.services.execution_intent_gateway",
    "app.tasks",
)


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_no_module_imports_an_order_or_broker_path(path: Path) -> None:
    offenders = [n for n in _imports(path) if n.startswith(FORBIDDEN)]
    assert offenders == []


def _module_path(name: str) -> Path | None:
    base = ROOT / name.replace(".", "/")
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def test_the_service_import_closure_reaches_no_broker_or_order_module() -> None:
    """Follow every ``app.services.*`` import (the data layer ``app.models`` and
    ``app.core`` are not followed) and refuse a broker or order module anywhere."""
    seen: set[str] = set()
    pending = [
        f"app.services.mandate.{p.stem}"
        for p in PACKAGE.glob("*.py")
        if p.stem != "__init__"
    ]
    offenders: list[str] = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_path(name)
        if path is None:
            continue
        for imported in _imports(path):
            if imported.startswith(FORBIDDEN):
                offenders.append(f"{name} -> {imported}")
            elif imported.startswith("app.services."):
                pending.append(imported)
    assert offenders == []
    # the closure really walked past the package into the sleeve store
    assert "app.services.strategy_sleeves.store" in seen


def test_the_package_writes_no_ledger_table() -> None:
    """Hard rule 5: the mandate store never touches an order ledger."""
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        for table in (
            "execution_ledger",
            "kis_live_order_ledger",
            "toss_live_order_ledger",
            "alpaca_paper_order_ledger",
        ):
            assert f"insert({table}" not in text.lower()
            assert f"INSERT INTO review.{table}" not in text


def test_i21_mta_callback_has_no_direct_order_or_proposal_imports() -> None:
    imports = _imports(PACKAGE / "telegram_approval.py")
    assert not any(
        name.startswith(("app.services.order_proposals", "app.models.order_proposals"))
        for name in imports
    )

"""hk 6508: the collector stays research-only: no trading, MCP or approval imports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "app" / "services" / "fnguide_consensus"
FORBIDDEN_PREFIXES = (
    "app.mcp_server",
    "app.services.brokers",
    "app.services.order_proposals",
    "app.services.kis",
    "app.services.toss",
    "app.services.upbit",
    "app.services.execution_ledger",
    "app.services.execution_intent_gateway",
    "app.services.strategy_sleeves",
    "app.services.mandate",
    "app.services.kiwoom_mock_place",
    "app.routers",
    "app.tasks",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_package_imports_no_trading_mcp_or_approval_modules() -> None:
    offenders = []
    for path in PACKAGE.glob("*.py"):
        for name in _imports(path):
            if name.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_cli_imports_no_trading_modules() -> None:
    cli = ROOT / "scripts" / "collect_fnguide_consensus.py"
    offenders = [n for n in _imports(cli) if n.startswith(FORBIDDEN_PREFIXES)]
    assert offenders == []


def test_no_mcp_tool_is_registered_for_fnguide() -> None:
    mcp_root = ROOT / "app" / "mcp_server"
    hits = [
        str(path)
        for path in mcp_root.rglob("*.py")
        if "fnguide" in path.read_text(encoding="utf-8").lower()
    ]
    assert hits == []

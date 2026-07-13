from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_ROOT = Path(__file__).resolve().parents[4]
_ADAPTER_PATHS = (
    _ROOT / "app/services/brokers/binance/paper_adapter.py",
    _ROOT / "app/services/brokers/alpaca/paper_adapter.py",
)
_COMMON_PATHS = tuple(sorted((_ROOT / "app/services/brokers/paper").glob("*.py")))
_ROB845_BOUNDARY_PATHS = (
    *_ADAPTER_PATHS,
    *_COMMON_PATHS,
    _ROOT / "app/services/alpaca_paper_order_application.py",
    _ROOT / "app/mcp_server/tooling/paper_execution_registration.py",
)
_EXPECTED_PROFILE_TOOLS = {
    "paper_execution_get_capabilities",
    "paper_execution_preview_order",
    "paper_execution_submit_order",
    "paper_execution_cancel_order",
    "paper_execution_get_order",
    "paper_execution_reconcile",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.module


def _assigned_names(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            )
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id


def test_production_adapters_do_not_call_raw_broker_submit() -> None:
    forbidden_calls = {"submit_order", "place_order", "_execute_http_request"}
    offenders: list[str] = []

    for path in _ADAPTER_PATHS:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else ""
            )
            if name in forbidden_calls:
                offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}:{name}")

    assert offenders == []


def test_production_adapters_import_no_live_or_mcp_tooling_surface() -> None:
    offenders: list[str] = []

    for path in _ADAPTER_PATHS:
        for module in _imports(_tree(path)):
            segments = module.lower().split(".")
            is_live = any(
                segment == "live"
                or segment.startswith("live_")
                or segment.endswith("_live")
                for segment in segments
            )
            if is_live or module.startswith("app.mcp_server"):
                offenders.append(f"{path.relative_to(_ROOT)} -> {module}")

    assert offenders == []


def test_common_layer_has_no_orm_repository_or_migration_boundary() -> None:
    forbidden_import_prefixes = ("sqlalchemy", "alembic", "app.models")
    forbidden_calls = {"Table", "Column", "mapped_column"}
    offenders: list[str] = []

    for path in _COMMON_PATHS:
        tree = _tree(path)
        for module in _imports(tree):
            if module.startswith(forbidden_import_prefixes) or "repository" in module:
                offenders.append(f"{path.relative_to(_ROOT)} -> {module}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if name in forbidden_calls:
                offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}:{name}")

    assert offenders == []
    assert not list((_ROOT / "app/models").glob("*rob845*"))
    assert not list((_ROOT / "alembic/versions").glob("*rob845*"))


def test_rob845_boundary_imports_no_rob848_or_rob849_implementation() -> None:
    forbidden_fragments = ("rob848", "rob_848", "rob849", "rob_849")
    offenders: list[str] = []

    for path in _ROB845_BOUNDARY_PATHS:
        for module in _imports(_tree(path)):
            lowered = module.lower()
            if any(fragment in lowered for fragment in forbidden_fragments):
                offenders.append(f"{path.relative_to(_ROOT)} -> {module}")

    assert offenders == []


@pytest.mark.parametrize(
    ("target_name", "expected_path"),
    [
        ("PAPER_EXECUTION", "app/mcp_server/profiles.py"),
        ("PAPER_BROKER_CAPABILITIES", "app/services/brokers/capabilities.py"),
    ],
)
def test_profile_and_capability_registry_have_one_source_of_truth(
    target_name: str,
    expected_path: str,
) -> None:
    definitions: list[str] = []

    for path in (_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if target_name in set(_assigned_names(_tree(path))):
            definitions.append(str(path.relative_to(_ROOT)))

    assert definitions == [expected_path]


def test_paper_execution_profile_is_exact_facade_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)
    mcp = DummyMCP()

    register_all_tools(mcp, profile=McpProfile.PAPER_EXECUTION)  # type: ignore[arg-type]

    assert set(mcp.tools) == _EXPECTED_PROFILE_TOOLS
    forbidden_fragments = {
        "alpaca_paper",
        "binance_demo",
        "kis",
        "kiwoom",
        "upbit",
        "toss",
        "live",
        "link_native",
    }
    assert not {
        name
        for name in mcp.tools
        if any(fragment in name for fragment in forbidden_fragments)
    }

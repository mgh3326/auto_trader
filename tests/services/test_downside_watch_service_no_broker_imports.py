"""ROB-928 safety-boundary guard.

The downside-watch mirror feature must only ever INSERT into
review.investment_watch_alerts (via DirectWatchCreateService) — it must
never import an order/broker mutation surface. This is a static AST
import scan, mirroring the style of
tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py,
scoped to the two ROB-928 modules.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

GUARDED_FILES: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "app" / "services" / "downside_watch_service.py",
    REPO_ROOT / "app" / "mcp_server" / "tooling" / "downside_watch_registration.py",
)

FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "app.services.brokers",
    "app.mcp_server.tooling.orders_",
    "app.mcp_server.tooling.order_proposal_tools",
    "app.mcp_server.tooling.alpaca_paper_orders",
    "app.mcp_server.tooling.live_order_ledger",
    "app.mcp_server.tooling.kis_live_ledger",
)

FORBIDDEN_NAME_SUBSTRINGS: tuple[str, ...] = (
    "place_order",
    "submit_order",
    "cancel_order",
    "modify_order",
    "preview_order",
)


def _imported_module_names(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


@pytest.mark.parametrize("path", GUARDED_FILES, ids=lambda p: p.name)
def test_no_broker_mutation_imports(path: pathlib.Path) -> None:
    assert path.exists(), f"expected ROB-928 module missing: {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = _imported_module_names(tree)

    for module in modules:
        for prefix in FORBIDDEN_MODULE_PREFIXES:
            assert not module.startswith(prefix), (
                f"{path.name} imports forbidden broker-mutation module {module!r}"
            )
        for needle in FORBIDDEN_NAME_SUBSTRINGS:
            assert needle not in module.lower(), (
                f"{path.name} imports a name suggesting order mutation: {module!r}"
            )

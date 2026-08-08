"""KR-scoped static import guard — no order-submission surface anywhere.

``tests/scripts/b0x/test_no_forbidden_imports.py`` already scans this whole
package for LLM/scheduler/live-order imports, but its ``FORBIDDEN_LIVE_ORDER``
tuple predates this lane and does not name any KIS module (crypto never
needed one). This file adds the KIS-specific list so ``scripts/b0x/kr/**`` is
held to the same standard, and documents *why* it currently passes: this PR
does not wire order submission at all (see ``scripts.b0x.kr.mock`` module
docstring) — ``KrMockSubmissionNotWired`` is the only thing standing in for
it. When submission is wired in a follow-up, whichever module is chosen
belongs in this list's "reviewed and accepted" companion, not a silent gap.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(__file__).resolve().parents[4] / "scripts" / "b0x" / "kr"
RUNNER = Path(__file__).resolve().parents[4] / "scripts" / "run_b0x_kr_cycle.py"

#: Anything capable of sending a KIS order — live *or* mock — plus the
#: MCP-tool-registration modules that define both in the same file. Reusing
#: AccountClient/BaseKISClient (reads only) is fine and expected; nothing in
#: this tuple is a read surface.
FORBIDDEN_KIS_ORDER_SURFACES = (
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.kis.client",
    "app.services.kis_trading_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_kis_variants",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
)


def _python_files() -> list[Path]:
    return sorted([*PACKAGE_ROOT.rglob("*.py"), RUNNER])


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_kr_package_actually_has_files() -> None:
    files = _python_files()
    assert len(files) >= 2, f"expected the kr package, found {files}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_kis_order_submission_surface_imported(path: Path) -> None:
    offenders = [
        module
        for module in _imported_modules(path)
        for forbidden in FORBIDDEN_KIS_ORDER_SURFACES
        if module == forbidden or module.startswith(f"{forbidden}.")
    ]
    assert offenders == [], f"{path.name} imports a KIS order surface: {offenders}"

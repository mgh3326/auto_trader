"""Static guard: the KRX official daily collector reaches no order, broker, ledger,
approval, MCP, router, task, or legacy KRX scraper module (hk 6586, acceptance 5).

The check follows imports transitively from the collector's own modules by parsing
source, so a new indirect path fails here even if no direct import changes.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
ENTRY_MODULES = (
    "app.services.krx_official_daily.client",
    "app.services.krx_official_daily.collector",
    "app.services.krx_official_daily.errors",
    "app.services.krx_official_daily.parse",
    "app.services.krx_official_daily.reader",
    "app.services.krx_official_daily.store",
    "app.models.krx_official_daily",
    "scripts.collect_krx_official_daily",
)
FORBIDDEN_PREFIXES = (
    "app.services.brokers",
    "app.services.order",
    "app.services.execution",
    "app.services.approval",
    "app.services.mandate",
    "app.services.strategy_sleeves",
    "app.services.kis",
    "app.services.kiwoom",
    "app.services.toss",
    "app.services.upbit",
    "app.services.binance",
    "app.services.nhplug",
    "app.services.decision_evidence",
    "app.services.kr_symbol_universe_service",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.flows",
    "app.services.krx_after_market",
    "app.services.krx_symbol_universe",
)
FORBIDDEN_EXACT = frozenset({"app.services.krx"})  # the data.krx.co.kr member scraper
# app/models/__init__.py re-exports every ORM model; it is a leaf here, not behavior.
LEAF_PACKAGES = frozenset({"app.models", "app.services"})


def _module_file(name: str) -> Path | None:
    parts = name.split(".")
    for candidate in (
        REPO.joinpath(*parts).with_suffix(".py"),
        REPO.joinpath(*parts, "__init__.py"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".")[0] in {"app", "scripts"}}


def _closure() -> set[str]:
    seen: set[str] = set()
    queue = deque(ENTRY_MODULES)
    while queue:
        name = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        if name in LEAF_PACKAGES:
            continue
        path = _module_file(name)
        if path is None:
            continue
        queue.extend(_imports(path))
    return seen


def _forbidden(name: str) -> bool:
    return name in FORBIDDEN_EXACT or any(
        name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES
    )


def test_collector_reaches_no_order_broker_ledger_or_approval_module() -> None:
    reached = _closure()
    assert "app.services.krx_official_daily.collector" in reached
    assert "app.models.krx_official_daily" in reached
    assert sorted(name for name in reached if _forbidden(name)) == []


def test_no_direct_write_to_order_or_ledger_tables_in_the_package() -> None:
    package = REPO / "app" / "services" / "krx_official_daily"
    text = "\n".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))
    for forbidden in (
        "order_ledger",
        "order_proposals",
        "execution_ledger",
        "kis_live",
    ):
        assert forbidden not in text

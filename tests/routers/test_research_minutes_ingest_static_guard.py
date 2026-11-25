"""hk 6343 static guard: the ingest surface reaches no order/ledger/approval path.

Acceptance 4. Two things are pinned:

1. Import closure — the router (module-level *and* function-level imports) plus
   the module-level imports of everything it imports reach no order, ledger,
   approval, sleeve or decision-evidence module. Function-level imports inside
   reused service modules are deferred code paths (the collector's screener
   snapshot read), so they are not part of a static binding — but a new direct
   binding at module level, or a new import in the router itself, is caught.
2. SQL surface — the only writer this router touches inserts into the two
   ``research`` minute tables, never ``public.*`` and never another table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.research_candles import session_minutes_collector as sc

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[2]
ROUTER = REPO / "app" / "routers" / "research_minutes_ingest.py"

#: Closed denylist: order submission, order/execution ledgers, approval,
#: sleeves, decision evidence and broker order clients.
DENIED_PREFIXES = (
    "app.services.order_proposals",
    "app.services.execution_ledger",
    "app.services.order_send_intent_service",
    "app.services.kis_live_order_ledger_service",
    "app.services.toss_live_order_ledger_service",
    "app.services.kiwoom_mock_order_ledger_service",
    "app.services.alpaca_paper_ledger_service",
    "app.services.mechanical_loss_cut",
    "app.services.mock_strategy",
    "app.services.strategy_sleeves",
    "app.services.decision_evidence",
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.toss",
    "app.services.brokers.upbit.orders",
    "app.services.brokers.kiwoom.domestic_orders",
    "app.services.brokers.kiwoom.us_orders",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.order_validation",
    "app.mcp_server.tooling.order_approval",
    "app.mcp_server.tooling.orders",
    "app.mcp_server.tooling.live_order_ledger",
    "app.mcp_server.tooling.kis_live_ledger",
    "app.models.order_proposals",
    "app.models.execution_ledger",
)


def _imports_of(path: Path, *, deep: bool) -> set[str]:
    tree = ast.parse(path.read_text())
    nodes = ast.walk(tree) if deep else tree.body
    found: set[str] = set()
    for node in nodes:
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.startswith("app")
        ):
            found.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    found.add(alias.name)
    return found


def _resolve(module: str) -> Path | None:
    base = REPO / module.replace(".", "/")
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return None


def _closure() -> dict[str, str | None]:
    """module -> the module that first imported it (None for the router)."""
    start = "app.routers.research_minutes_ingest"
    parent: dict[str, str | None] = {start: None}
    stack = [start]
    while stack:
        module = stack.pop()
        path = _resolve(module)
        if path is None:
            continue
        for imported in _imports_of(path, deep=module == start):
            if imported in parent:
                continue
            parent[imported] = module
            stack.append(imported)
    return parent


def _chain(parent: dict[str, str | None], module: str) -> str:
    parts: list[str] = []
    current: str | None = module
    while current is not None:
        parts.append(current)
        current = parent[current]
    return " <- ".join(parts)


def test_the_router_import_closure_holds_no_order_ledger_or_approval_module() -> None:
    parent = _closure()

    leaked = {
        module: _chain(parent, module)
        for module in parent
        if module.startswith(DENIED_PREFIXES)
    }
    assert leaked == {}, f"order/ledger/approval modules reached: {leaked}"

    # A denylist can only be trusted when every app import resolved, or names a
    # real module's attribute (``from x import y``). Anything whose own dotted
    # parent does not exist is a typo that would make the guard blind.
    unresolved = [
        module
        for module in parent
        if _resolve(module) is None and _resolve(module.rsplit(".", 1)[0]) is None
    ]
    assert unresolved == []


def test_the_router_imports_the_shared_writer_and_nothing_that_submits() -> None:
    source = ROUTER.read_text()
    assert "SqlMinuteWriter" in source
    assert "insert_one" in source
    for forbidden in ("place_order", "cancel_order", "send_order", "submit_order"):
        assert forbidden not in source


def test_the_only_writer_statements_target_the_research_minute_tables() -> None:
    sql = {
        "kr": str(sc._KR_INSERT),
        "us": str(sc._US_INSERT),
    }
    assert "INSERT INTO research.kr_candles_1m" in sql["kr"]
    assert "INSERT INTO research.us_candles_1m" in sql["us"]
    for statement in sql.values():
        assert "public." not in statement
        assert statement.count("INSERT INTO") == 1
        assert "ON CONFLICT (time_utc, symbol, venue) DO NOTHING" in statement
        assert not re.search(r"\b(UPDATE|DELETE)\b", statement)

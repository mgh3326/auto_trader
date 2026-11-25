"""hk 6343 static guard: the ingest surface reaches no order/ledger/approval path.

Acceptance 4. What is pinned:

1. Import closure — the router (module-level *and* function-level imports) plus
   the module-level imports of everything it imports reach no order, ledger,
   approval, sleeve or decision-evidence module, **except** the reviewed KIS
   holdings exception below. Relative imports are resolved against the importing
   package and parent ``__init__`` modules are walked, so the closure sees what
   importing the module really executes.
2. Reviewed exception — reading KIS holdings needs ``app.services.brokers.kis``,
   whose package ``__init__`` imports its two order clients (the collector's
   ``session_segment`` already depends on that package for the KR session
   constants, hk 6145). The order clients reached through that package are
   listed explicitly, the router wraps the client in a holdings-only view, and a
   *new* order module reached anywhere else still fails this test.
3. SQL surface — the only writer this router touches inserts into the two
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
START = "app.routers.research_minutes_ingest"

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

#: The reviewed exception (hk 6343 review). Importing ANY
#: ``app.services.brokers.kis`` submodule runs that package's ``__init__``,
#: which imports its order clients; ``session_segment`` (hk 6145) already
#: imports ``...kis.live_order_expiry`` at module level. So order modules can be
#: reached through that package — and only there. The runtime surface is
#: narrowed to the two holdings reads (``_HoldingsOnlyReader``) by the tests
#: below: an order module reached from anywhere outside this root fails.
REVIEWED_REACH_ROOT = "app.services.brokers.kis"


def _module_file(name: str) -> Path | None:
    base = REPO / name.replace(".", "/")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    return None


def _imports_of(path: Path, importing: str, *, deep: bool) -> set[str]:
    """``app.*`` imports of ``path``; relative imports resolve to real modules."""
    tree = ast.parse(path.read_text())
    nodes = ast.walk(tree) if deep else tree.body
    is_package = path.name == "__init__.py"
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # A package's ``__init__`` resolves ``.x`` against itself;
                # a module resolves it against its parent package.
                drop = node.level - 1 if is_package else node.level
                base = importing if drop == 0 else importing.rsplit(".", drop)[0]
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if not module.startswith("app"):
                continue
            found.add(module)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    found.add(alias.name)
    return found


def _closure() -> dict[str, str | None]:
    """module -> the module that first imported it (None for the router)."""
    parent: dict[str, str | None] = {START: None}
    stack = [START]
    while stack:
        module = stack.pop()
        path = _module_file(module)
        if path is None:
            continue
        # Importing a.b.c also executes the a and a.b packages (rule 3).
        parts = module.split(".")
        parents = [".".join(parts[:end]) for end in range(1, len(parts))]
        neighbours = set(_imports_of(path, module, deep=module == START))
        neighbours.update(p for p in parents if p.startswith("app"))
        for imported in neighbours:
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


def _reached_through(parent: dict[str, str | None], module: str, root: str) -> bool:
    """True when some ancestor of ``module`` (not itself) is under ``root``."""
    current = parent.get(module)
    while current is not None:
        if current == root or current.startswith(f"{root}."):
            return True
        current = parent[current]
    return False


def test_relative_imports_resolve_to_real_modules() -> None:
    """The old helper skipped every relative import (``node.level == 0``).

    ``app/services/brokers/kis/client.py`` imports both order clients relatively,
    so this must name them — otherwise the closure below is blind to what
    importing the KIS client actually loads.
    """
    found = _imports_of(
        REPO / "app" / "services" / "brokers" / "kis" / "client.py",
        "app.services.brokers.kis.client",
        deep=False,
    )
    assert "app.services.brokers.kis.domestic_orders" in found
    assert "app.services.brokers.kis.overseas_orders" in found


def test_the_router_import_closure_holds_no_unreviewed_order_module() -> None:
    parent = _closure()

    denied = sorted(module for module in parent if module.startswith(DENIED_PREFIXES))
    # Every denied module must be reached only through the reviewed KIS package
    # reach (which the router enters lazily and wraps). Any denied module with
    # another ancestor is a new binding and fails here with its chain.
    offenders = {
        module: _chain(parent, module)
        for module in denied
        if not _reached_through(parent, module, REVIEWED_REACH_ROOT)
    }
    assert offenders == {}, offenders

    # The KIS read client is entered from this router only, and lazily (the
    # import sits inside the targets handler).
    assert parent.get("app.services.brokers.kis.client") == START

    # A denylist can only be trusted when every app import resolved, or names a
    # real module's attribute (``from x import y``). Anything whose own dotted
    # parent does not exist is a typo that would make the guard blind.
    unresolved = [
        module
        for module in parent
        if _module_file(module) is None
        and _module_file(module.rsplit(".", 1)[0]) is None
    ]
    assert unresolved == []


def _class_methods(source: str, class_name: str) -> list[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                child.name
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
    raise AssertionError(f"{class_name} not found")


def test_the_targets_reader_exposes_only_the_two_holdings_reads() -> None:
    source = ROUTER.read_text()
    public = {
        name
        for name in _class_methods(source, "_HoldingsOnlyReader")
        if not name.startswith("_")
    }
    assert public == {"fetch_my_stocks", "fetch_my_us_stocks"}

    # The KIS client is constructed in the targets handler only, and only
    # through that wrapper.
    assert source.count("KISClient") >= 1
    assert "_HoldingsOnlyReader(" in source
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


def test_the_guard_would_catch_a_new_direct_order_import() -> None:
    """Sanity: the denylist really matches the module names it names."""
    for module in (
        "app.services.order_proposals.dispatch",
        "app.services.execution_ledger.repository",
        "app.services.brokers.kis.domestic_orders",
        "app.mcp_server.tooling.order_execution",
    ):
        assert module.startswith(DENIED_PREFIXES)
    for safe in (
        "app.services.brokers.kis.live_order_expiry",
        "app.services.research_candles.session_segment",
    ):
        assert not safe.startswith(DENIED_PREFIXES)

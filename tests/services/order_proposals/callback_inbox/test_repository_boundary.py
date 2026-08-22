"""The inbox repository is reachable only through its service.

Mirrors ``tests/services/order_proposals/test_no_repository_imports.py``. The
service is where the scrub, the attempt accounting and the claim rules live;
a second writer that went straight to the repository would bypass all three,
and a terminal row written without the scrub is exactly the failure the DB
CHECK exists to catch second, not first.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_BANNED = "app.services.order_proposals.callback_inbox.repository"
_ALLOWED = {
    pathlib.Path("app/services/order_proposals/callback_inbox/service.py"),
}


def _is_banned(module: str | None) -> bool:
    if not module:
        return False
    return module == _BANNED or module.startswith(_BANNED + ".")


def _import_from_modules(path: pathlib.Path, node: ast.ImportFrom) -> set[str]:
    """Resolve every absolute module named by one ``from`` import."""
    if node.level:
        package = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
        parents = node.level - 1
        if parents > len(package):
            return set()
        prefix = package[: len(package) - parents]
        if node.module:
            prefix = (*prefix, *node.module.split("."))
        module = ".".join(prefix)
    else:
        module = node.module or ""

    names = {module} if module else set()
    names.update(
        f"{module}.{alias.name}" if module else alias.name for alias in node.names
    )
    return names


def _imported_modules_from_tree(path: pathlib.Path, tree: ast.AST) -> set[str]:
    """Return absolute module names, including relative-import aliases."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(_import_from_modules(path, node))
    return names


def _imports_repo(path: pathlib.Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    return any(_is_banned(module) for module in _imported_modules_from_tree(path, tree))


def test_repository_import_boundary_enforced() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "app").rglob("*.py")
        if path.relative_to(REPO_ROOT) not in _ALLOWED and _imports_repo(path)
    ]
    assert not offenders, f"inbox repository imported outside its service: {offenders}"


def test_service_actually_imports_the_repository() -> None:
    service = REPO_ROOT / "app/services/order_proposals/callback_inbox/service.py"
    assert _imports_repo(service), "guard would be vacuous"


#: Roots that lead to a broker, an order or a ledger write. The inbox decides
#: *when* the existing callback core runs; it must never become a second way
#: to reach a mutation.
FORBIDDEN_EXECUTION_ROOTS: tuple[str, ...] = (
    "app.services.brokers",
    "app.mcp_server.tooling.orders_",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.live_order_ledger",
    "app.mcp_server.tooling.live_order_evidence",
    "app.mcp_server.tooling.kis_live_ledger",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.auto_approve",
    "app.services.order_proposals.resting_sweep",
    "app.services.kis_trading_service",
    "app.services.kis_holdings_service",
    "app.services.order_send_intent_service",
    "app.services.alpaca_paper_ledger_service",
    "app.services.toss_live_order_ledger_service",
    "app.services.execution_ledger",
    "app.models.review",
    "app.models.binance_demo_order_ledger",
)

#: Every module the closed world covers.
GUARDED_MODULES: tuple[pathlib.Path, ...] = (
    *sorted((REPO_ROOT / "app/services/order_proposals/callback_inbox").rglob("*.py")),
    REPO_ROOT / "app/tasks/telegram_callback_inbox_tasks.py",
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Every module name any import in this file brings in, alias by alias."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _imported_modules_from_tree(path, tree)


def test_relative_imports_cannot_bypass_either_guard() -> None:
    """Anti-vacuity for package-local and parent-relative import spellings."""
    logical_path = (
        REPO_ROOT / "app/services/order_proposals/callback_inbox/probe_module.py"
    )
    tree = ast.parse(
        "from .repository import CallbackInboxRepository\n"
        "from . import repository\n"
        "from .. import revalidation\n"
    )
    modules = _imported_modules_from_tree(logical_path, tree)
    assert _BANNED in modules
    assert "app.services.order_proposals.revalidation" in modules
    assert any(_is_banned(module) for module in modules)


def test_the_closed_world_covers_the_task_module_too() -> None:
    """R8 B17 — the TaskIQ entrypoint is part of the inbox's surface."""
    assert (REPO_ROOT / "app/tasks/telegram_callback_inbox_tasks.py") in (
        GUARDED_MODULES
    )
    assert len(GUARDED_MODULES) >= 8, GUARDED_MODULES
    for path in GUARDED_MODULES:
        assert path.exists(), path


def test_no_guarded_module_imports_an_execution_surface() -> None:
    """R8 B17 — every alias of every import, not just the first name."""
    offenders: list[str] = []
    for path in GUARDED_MODULES:
        for module in _imported_modules(path):
            for root in FORBIDDEN_EXECUTION_ROOTS:
                if module == root.rstrip("_") or module.startswith(root):
                    offenders.append(f"{path.name}: {module}")
    assert not offenders, offenders


def test_the_guard_would_notice_a_second_mutation_path() -> None:
    """Anti-vacuity: the matcher really does catch what it claims to."""
    probes = (
        "app.services.brokers.kis.client",
        "app.mcp_server.tooling.orders_toss_variants",
        "app.mcp_server.tooling.order_execution",
        "app.services.order_proposals.revalidation",
        "app.models.review",
    )
    for probe in probes:
        assert any(
            probe == root.rstrip("_") or probe.startswith(root)
            for root in FORBIDDEN_EXECUTION_ROOTS
        ), probe


def test_only_the_callback_core_seam_leads_to_a_mutation() -> None:
    """The one permitted door, named exactly once, in one module."""
    core_importers = [
        path.name
        for path in GUARDED_MODULES
        if any(
            module.startswith("app.services.order_proposals.telegram_callback")
            for module in _imported_modules(path)
        )
    ]
    assert core_importers == ["ingress.py", "worker.py"], core_importers

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


def _imports_repo(path: pathlib.Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_banned(node.module):
            return True
        if isinstance(node, ast.Import) and any(
            _is_banned(alias.name) for alias in node.names
        ):
            return True
    return False


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


def test_the_inbox_reaches_no_broker_or_ledger_surface() -> None:
    """The inbox decides *when* the callback core runs, never *what* it does.

    Anything broker-, ledger- or order-execution-shaped imported here would
    mean a second path to a mutation that does not go through the callback
    core's gates.
    """
    package = REPO_ROOT / "app/services/order_proposals/callback_inbox"
    forbidden = (
        "app.services.brokers",
        "app.mcp_server.tooling.order_execution",
        "app.services.order_proposals.broker_gateway",
        "app.services.order_proposals.revalidation",
        "app.services.kis_trading_service",
        "app.services.order_send_intent_service",
    )
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and any(module.startswith(item) for item in forbidden):
                offenders.append(f"{path.name}:{module}")
    assert not offenders, offenders

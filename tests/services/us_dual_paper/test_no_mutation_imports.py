import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GUARDED = REPO_ROOT / "app" / "services" / "us_dual_paper"

# Modules that imply order mutation or live-KIS routing must never be imported here.
_BANNED_PREFIXES = (
    "app.mcp_server.tooling.order_execution",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.kis.domestic_orders",
    "app.services.kis_trading_service",
    "app.services.brokers.alpaca.orders",
    "app.mcp_server.tooling.alpaca_paper_orders",
)
# Symbols that must never appear as ImportFrom names.
_BANNED_NAMES = {
    "submit_order",
    "place_order",
    "cancel_order",
    "modify_order",
    "_place_order_impl",
}


def _is_banned_module(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _BANNED_PREFIXES)


def _py_files():
    return sorted(GUARDED.rglob("*.py"))


def test_guarded_dir_exists():
    assert GUARDED.is_dir()
    assert _py_files()


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p))
def test_no_banned_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if _is_banned_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _is_banned_module(mod):
                offenders.append(mod)
            offenders += [a.name for a in node.names if a.name in _BANNED_NAMES]
    assert not offenders, (
        f"{path} imports forbidden mutation/live surfaces: {offenders}"
    )

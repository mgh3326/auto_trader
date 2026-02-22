from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_FACADES = {
    "app.services.kis",
    "app.services.upbit",
    "app.services.yahoo",
}
TARGET_DIRS = (
    ROOT / "app" / "jobs",
    ROOT / "app" / "routers",
    ROOT / "app" / "mcp_server" / "tooling",
)
TOOLING_ALLOWLIST = {
    "app/mcp_server/tooling/order_execution.py",
    "app/mcp_server/tooling/portfolio_cash.py",
    "app/mcp_server/tooling/portfolio_holdings.py",
}


def _find_legacy_facade_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in LEGACY_FACADES:
                    hits.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in LEGACY_FACADES:
                hits.add(module)

    return sorted(hits)


def _find_imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def _find_integration_imports(path: Path) -> list[str]:
    return sorted(
        module
        for module in _find_imported_modules(path)
        if module.startswith("app.integrations")
    )


def test_no_legacy_facade_imports_in_jobs_and_routers() -> None:
    violations: dict[str, list[str]] = {}
    for directory in TARGET_DIRS[:2]:
        for path in directory.rglob("*.py"):
            imports = _find_legacy_facade_imports(path)
            if imports:
                rel = path.relative_to(ROOT).as_posix()
                violations[rel] = imports

    assert violations == {}


def test_tooling_legacy_facade_imports_are_allowlisted() -> None:
    violations: dict[str, list[str]] = {}
    for path in (TARGET_DIRS[2]).rglob("*.py"):
        imports = _find_legacy_facade_imports(path)
        if imports:
            rel = path.relative_to(ROOT).as_posix()
            violations[rel] = imports

    unexpected = {
        path: imports
        for path, imports in violations.items()
        if path not in TOOLING_ALLOWLIST
    }
    assert unexpected == {}


def test_screener_job_no_longer_imports_kis_facade() -> None:
    screener_path = ROOT / "app" / "jobs" / "screener.py"
    modules = _find_imported_modules(screener_path)

    assert "app.integrations.kis" not in modules
    assert "app.services.kis" not in modules
    assert "app.services" in modules


def test_watch_scanner_job_no_direct_integration_imports() -> None:
    watch_scanner_path = ROOT / "app" / "jobs" / "watch_scanner.py"
    assert _find_integration_imports(watch_scanner_path) == []

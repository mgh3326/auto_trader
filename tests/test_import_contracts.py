from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED_SERVICE_IMPORT_NAMES = {"kis", "upbit", "yahoo"}
BANNED_INTEGRATION_IMPORT_NAMES = {"kis", "upbit", "yahoo"}
KIS_PROVIDER = "kis"
BANNED_MODULES = {f"app.services.{name}" for name in BANNED_SERVICE_IMPORT_NAMES} | {
    f"app.integrations.{name}" for name in BANNED_INTEGRATION_IMPORT_NAMES
}
TARGET_DIRS = (
    ROOT / "app" / "jobs",
    ROOT / "app" / "routers",
    ROOT / "app" / "mcp_server" / "tooling",
)


def _find_banned_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BANNED_MODULES:
                    hits.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in BANNED_MODULES:
                hits.add(module)
            if module == "app.services":
                for alias in node.names:
                    if alias.name in BANNED_SERVICE_IMPORT_NAMES:
                        hits.add(f"{module}:{alias.name}")
            if module == "app.integrations":
                for alias in node.names:
                    if alias.name in BANNED_INTEGRATION_IMPORT_NAMES:
                        hits.add(f"{module}:{alias.name}")

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


def test_no_banned_provider_imports_in_runtime_paths() -> None:
    violations: dict[str, list[str]] = {}
    for directory in TARGET_DIRS:
        for path in directory.rglob("*.py"):
            imports = _find_banned_imports(path)
            if imports:
                rel = path.relative_to(ROOT).as_posix()
                violations[rel] = imports

    assert violations == {}


def test_screener_job_no_longer_imports_kis_facade() -> None:
    screener_path = ROOT / "app" / "jobs" / "screener.py"
    modules = _find_imported_modules(screener_path)

    assert f"app.integrations.{KIS_PROVIDER}" not in modules
    assert f"app.services.{KIS_PROVIDER}" not in modules
    assert "app.services" in modules


def test_watch_scanner_job_no_direct_integration_imports() -> None:
    watch_scanner_path = ROOT / "app" / "jobs" / "watch_scanner.py"
    assert _find_integration_imports(watch_scanner_path) == []

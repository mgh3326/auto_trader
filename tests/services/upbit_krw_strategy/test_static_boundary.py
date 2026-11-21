"""The rule library is pure: no DB, broker, network, settings or scheduler."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[3] / "app" / "services" / "upbit_krw_strategy"
REPO = PACKAGE.parents[3]

ALLOWED_TOP_LEVEL = {
    "__future__", "collections", "dataclasses", "datetime", "decimal",
    "hashlib", "json", "typing",
}  # fmt: skip
ALLOWED_ABSOLUTE = {
    "app.core.upbit_tick",
    "research.crypto_universe.universe",
}
FORBIDDEN_LOADED = (
    "sqlalchemy", "httpx", "redis", "taskiq", "pandas", "numpy", "requests",
    "app.core.config", "app.core.db", "app.models", "app.services.brokers",
    "app.mcp_server", "app.tasks", "app.services.execution_intent_gateway",
)  # fmt: skip


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_every_import_is_stdlib_relative_or_allow_listed() -> None:
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        for module, level in _imports(path):
            if level:
                continue
            if module in ALLOWED_ABSOLUTE or module.split(".")[0] in ALLOWED_TOP_LEVEL:
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == []


def test_importing_the_package_loads_no_io_modules() -> None:
    code = (
        "import sys\n"
        "import app.services.upbit_krw_strategy.universe\n"
        "import app.services.upbit_krw_strategy.signals\n"
        "import app.services.upbit_krw_strategy.sizing\n"
        "import app.services.upbit_krw_strategy.exits\n"
        "import app.services.upbit_krw_strategy.selection\n"
        f"bad = [m for m in sys.modules if any(m == p or m.startswith(p + '.') "
        f"for p in {FORBIDDEN_LOADED!r})]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""

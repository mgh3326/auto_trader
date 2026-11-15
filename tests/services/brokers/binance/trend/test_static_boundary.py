"""CT-3 has no send path: static guards over the trend package and its callers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[5]
PACKAGE = ROOT / "app/services/brokers/binance/trend"
PACKAGE_MODULE = "app.services.brokers.binance.trend"

# First-party imports the package may use: its own modules and its ORM models.
ALLOWED_FIRST_PARTY = ("app.models.binance_trend", PACKAGE_MODULE)
# Anything that can open a socket or talk to a broker.
FORBIDDEN_THIRD_PARTY = (
    "httpx",
    "aiohttp",
    "requests",
    "websockets",
    "websocket",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "asyncpg",
    "redis",
    "taskiq",
    "prefect",
)


def _imports(path: Path, *, in_package: bool = False) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                if in_package:
                    # Relative imports stay inside the package by construction.
                    found.append(
                        (f"{PACKAGE_MODULE}.{module}".rstrip("."), node.lineno)
                    )
            else:
                found.append((module, node.lineno))
                found += [
                    (f"{module}.{alias.name}", node.lineno) for alias in node.names
                ]
    return found


def _package_files() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} >= {"__init__.py", "constants.py", "state.py"}
    return files


def test_the_package_imports_no_execution_client_order_module_or_network_library() -> (
    None
):
    offenders: list[str] = []
    for path in _package_files():
        for module, line in _imports(path, in_package=True):
            top = module.split(".")[0]
            # `from app.models import X` would hide a model import: refuse it too.
            first_party_ok = module.startswith(ALLOWED_FIRST_PARTY)
            if (top == "app" and not first_party_ok) or (
                top != "app"
                and (
                    module.startswith(FORBIDDEN_THIRD_PARTY)
                    or top in {"research", "scripts"}
                )
            ):
                offenders.append(f"{path.name}:{line} {module}")
    assert not offenders


def test_the_package_never_references_an_order_submission_name() -> None:
    banned = ("submit_order", "place_order", "new_order", "create_order", "newOrder")
    for path in _package_files():
        source = path.read_text()
        for name in banned:
            assert name not in source, f"{path.name} mentions {name}"


def test_nothing_outside_the_package_imports_it() -> None:
    """No CLI, task, scheduler or other module reaches the trend package yet."""
    offenders: list[str] = []
    for base in ("app", "scripts", "research"):
        for path in (ROOT / base).rglob("*.py"):
            if PACKAGE in path.parents:
                continue
            for module, _ in _imports(path):
                if module.startswith(PACKAGE_MODULE):
                    offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders


def test_the_state_service_is_the_only_writer_of_the_trend_tables() -> None:
    """Only the models, the migration, bootstrap and ``state.py`` name the tables."""
    markers = (
        "BinanceTrendSignal",
        "BinanceTrendIntent",
        "binance_trend_signals",
        "binance_trend_intents",
    )
    allowed = {
        PACKAGE / "state.py",
        ROOT / "app/models/binance_trend.py",
        ROOT / "app/models/__init__.py",
    }
    offenders = [
        str(path.relative_to(ROOT))
        for base in ("app", "scripts", "research")
        for path in (ROOT / base).rglob("*.py")
        if path not in allowed and any(m in path.read_text() for m in markers)
    ]
    assert not offenders


def test_the_package_has_no_env_gate_scheduler_or_cli_surface() -> None:
    for path in _package_files():
        source = path.read_text()
        for needle in ("os.environ", "getenv", "argparse", "get_settings", "settings."):
            assert needle not in source, f"{path.name} reads {needle}"
        assert "__main__" not in source
    assert not list(ROOT.glob("scripts/binance_trend*"))

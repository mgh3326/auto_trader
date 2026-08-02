"""ROB-1036 §4.2-9 / §4.3-11 — service-layer-only writes, offline modules.

Static AST guards, modelled on
``tests/services/order_proposals/test_no_repository_imports.py``. No DB,
broker, network, or account access anywhere in this file.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "app/services/invalid_sample_eligibility"

_REPOSITORY_MODULE = "app.services.invalid_sample_eligibility.repository"
_REPOSITORY_ALLOWED_IMPORTERS = {
    pathlib.Path("app/services/invalid_sample_eligibility/service.py")
}

_ELIGIBILITY_TABLES = (
    "sample_eligibility_decisions",
    "invalid_sample_cleanup_bindings",
    "invalid_sample_cleanup_lifecycle_events",
)
_WRITE_SQL_TOKENS = ("insert into", "update ", "delete from", "truncate ")

# Pure modules must not reach a database, a broker, the network, or a clock.
_PURE_MODULES = ("contract.py", "post_fill.py", "binding.py")
_PURE_FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "httpx",
    "requests",
    "aiohttp",
    "app.core.db",
    "app.services.brokers",
)


def _module_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _imports_repository(path: pathlib.Path) -> bool:
    return any(
        name == _REPOSITORY_MODULE or name.startswith(_REPOSITORY_MODULE + ".")
        for name in _module_names(path)
    )


def test_repository_is_imported_only_by_the_service() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "app").rglob("*.py")
        if path.relative_to(REPO_ROOT) not in _REPOSITORY_ALLOWED_IMPORTERS
        and _imports_repository(path)
    ]
    assert not offenders, f"repository imported outside its service: {offenders}"


def test_service_actually_imports_the_repository() -> None:
    """Without this the boundary guard above would be vacuous."""

    assert _imports_repository(PACKAGE / "service.py")


def test_pure_modules_have_no_io_dependencies() -> None:
    offenders: list[tuple[str, str]] = []
    for module in _PURE_MODULES:
        for name in _module_names(PACKAGE / module):
            if any(name.startswith(prefix) for prefix in _PURE_FORBIDDEN_PREFIXES):
                offenders.append((module, name))
    assert not offenders, f"pure module reached I/O: {offenders}"


def test_no_raw_sql_writes_to_the_eligibility_tables_anywhere_in_app() -> None:
    """Every write goes through the ORM models via the service layer."""

    offenders: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        lowered = path.read_text(encoding="utf-8").lower()
        for table in _ELIGIBILITY_TABLES:
            if table not in lowered:
                continue
            for token in _WRITE_SQL_TOKENS:
                if token in lowered:
                    # Only flag when the write token and the table name share a line.
                    for line in lowered.splitlines():
                        if table in line and token in line:
                            offenders.append((str(path.relative_to(REPO_ROOT)), line))
    assert not offenders, f"raw SQL write against an append-only table: {offenders}"


def test_service_never_updates_or_deletes() -> None:
    """The service module contains no ORM update/delete construct."""

    source = (PACKAGE / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"update", "delete", "merge", "execute"}
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
    ]
    assert not offenders, f"service performs a mutation call: {offenders}"


def test_repository_exposes_no_update_or_delete_method() -> None:
    tree = ast.parse((PACKAGE / "repository.py").read_text(encoding="utf-8"))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert not any(
        name.startswith(("update_", "delete_", "remove_", "purge_"))
        for name in method_names
    ), method_names


def test_package_never_imports_a_broker_or_order_surface() -> None:
    forbidden = ("app.services.brokers", "app.mcp_server", "app.services.alpaca_paper")
    offenders: list[tuple[str, str]] = []
    for path in PACKAGE.rglob("*.py"):
        for name in _module_names(path):
            if any(name.startswith(prefix) for prefix in forbidden):
                offenders.append((path.name, name))
    assert not offenders, f"eligibility package reached a broker surface: {offenders}"

"""Nothing but the manual CLI may wake a collection (hk 6409, hard rule 6).

Factortrail reads over retro/v1; no router, MCP tool, retro reader, task, job or
scheduler may reach the backfill package or its CLI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "app/services/market_events/backfill"
CLI = ROOT / "scripts/request_market_event_backfill.py"
TOKENS = ("app.services.market_events.backfill", "request_market_event_backfill")
#: Anything the backfill package or the collection read must not import.
FORBIDDEN_IMPORTS = (
    "app.services.brokers",
    "app.services.order",
    "app.services.execution",
    "app.mcp_server",
    "app.routers",
    "app.tasks",
    "app.flows",
    "app.jobs",
    "taskiq",
    "prefect",
)


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


def test_only_the_cli_imports_the_backfill_package() -> None:
    importers = set()
    for base in ("app", "scripts", "research"):
        for path in (ROOT / base).rglob("*.py"):
            if PACKAGE in path.parents:
                continue
            if any(name.startswith(TOKENS) for name in imports(path)):
                importers.add(path)
    assert importers == {CLI}


def test_no_module_dockerfile_workflow_or_unit_names_the_cli() -> None:
    """A subprocess or cron line would not show up as an import."""
    offenders = []
    files = [*ROOT.glob("Dockerfile*"), ROOT / "Makefile"]
    for base in ("app", "scripts", "deploy", ".github", "ops", "config"):
        files += [p for p in (ROOT / base).rglob("*") if p.is_file()]
    for path in files:
        if path == CLI or PACKAGE in path.parents or not path.is_file():
            continue
        if path.suffix == ".pyc":
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        if "request_market_event_backfill" in body:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_package_and_the_collection_read_reach_no_order_or_scheduler() -> None:
    files = [
        *PACKAGE.glob("*.py"),
        ROOT / "app/services/market_events/collection.py",
        CLI,
    ]
    assert len(files) == 6
    offenders = [
        f"{path.name}: {name}"
        for path in files
        for name in imports(path)
        if name.startswith(FORBIDDEN_IMPORTS)
    ]
    assert offenders == []


def test_the_collection_read_imports_no_writer() -> None:
    names = imports(ROOT / "app/services/market_events/collection.py")
    assert not [
        n
        for n in names
        if n.startswith(
            (
                "app.services.market_events.repository",
                "app.services.market_events.ingestion",
                "app.services.market_events.backfill",
            )
        )
    ]


def test_the_request_table_has_one_writer() -> None:
    writers = set()
    for base in ("app", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if path.name == "market_event_backfill.py" and path.parent.name == "models":
                continue
            body = path.read_text(encoding="utf-8")
            if "MarketEventBackfillRequest" in body and path.name != "__init__.py":
                writers.add(path.relative_to(ROOT).as_posix())
    assert writers == {"app/services/market_events/backfill/store.py"}
    store = (PACKAGE / "store.py").read_text(encoding="utf-8")
    for statement in ("update(", "delete(", "UPDATE ", "DELETE ", "TRUNCATE"):
        assert statement not in store

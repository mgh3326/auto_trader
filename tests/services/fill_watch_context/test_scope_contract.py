"""Static scope proof for the orderless #137 context boundary."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from app.models.fill_watch_context_outcome import _CONTEXTUAL_STATUSES
from app.services.fill_watch_context.domain import CONTEXTUAL_STATUS_VALUES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
CONTEXT_SOURCES = (
    ROOT / "app/services/fill_watch_context",
    ROOT / "app/mcp_server/tooling/fill_watch_context_registration.py",
    ROOT / "scripts/fill_watch_context_consumer.py",
    ROOT / "scripts/fill_watch_context_shadow_replay.py",
)
PROTECTED_EXISTING_SURFACES = (
    ROOT / "app/services/brokers/toss/auth.py",
    ROOT / "app/services/brokers/toss/client.py",
    ROOT / "app/services/brokers/token_issuance.py",
    ROOT / "app/services/order_proposals/auto_approve.py",
    ROOT / "app/services/order_proposals/service.py",
    ROOT / "app/mcp_server/tooling/order_execution.py",
    ROOT / "app/mcp_server/tooling/order_proposal_tools.py",
    ROOT / "app/services/fill_event_handoff/service.py",
    ROOT / "app/services/watch_trigger_repricing/chain_spawner.py",
    ROOT / "app/services/watch_trigger_repricing/proposal_chain.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "app.services.brokers",
    "app.tasks",
    "app.services.watch_trigger_repricing",
    "app.mcp_server.tooling.orders",
    "app.mcp_server.tooling.order_proposal",
    "app.mcp_server.tooling.investment_reports",
)


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _context_python_paths() -> list[Path]:
    paths: list[Path] = []
    for source in CONTEXT_SOURCES:
        if source.is_dir():
            paths.extend(sorted(source.rglob("*.py")))
        else:
            paths.append(source)
    return paths


def test_context_sources_have_no_execution_auth_or_scheduler_import_path() -> None:
    violations: list[str] = []
    for path in _context_python_paths():
        for module in _imports(path):
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{path.relative_to(ROOT)}: {module}")
    assert not violations


def test_existing_auth_and_execution_surfaces_do_not_wire_back_into_context() -> None:
    """Static complement to the delivery scope diff recorded for this PR."""
    references = [
        str(path.relative_to(ROOT))
        for path in PROTECTED_EXISTING_SURFACES
        if "fill_watch_context" in path.read_text(encoding="utf-8")
    ]
    assert references == []


def test_scope_diff_leaves_existing_guard_surfaces_unchanged() -> None:
    """Check committed, staged, and working-tree changes against the PR base."""
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    changed = set(
        subprocess.run(
            ["git", "diff", "--name-only", merge_base],
            check=True,
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    protected = {str(path.relative_to(ROOT)) for path in PROTECTED_EXISTING_SURFACES}
    assert not changed & protected


def test_database_check_vocabulary_matches_the_only_context_status_enum() -> None:
    assert _CONTEXTUAL_STATUSES == CONTEXTUAL_STATUS_VALUES
    forbidden = {"proposal_created", "submitted", "executed", "approved"}
    assert forbidden.isdisjoint(CONTEXTUAL_STATUS_VALUES)


def test_context_outcome_rows_are_constructed_only_in_the_service_layer() -> None:
    constructors: list[str] = []
    for path in _context_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FillWatchContextOutcome"
            for node in ast.walk(tree)
        ):
            constructors.append(str(path.relative_to(ROOT)))
    assert constructors == ["app/services/fill_watch_context/service.py"]


def test_context_entrypoint_cannot_spawn_or_poll() -> None:
    forbidden_modules = ("subprocess", "taskiq", "httpx", "urllib")
    for filename in (
        "fill_watch_context_consumer.py",
        "fill_watch_context_shadow_replay.py",
    ):
        path = ROOT / "scripts" / filename
        imports = _imports(path)
        assert not [
            module for module in imports if module.startswith(forbidden_modules)
        ]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not [node for node in ast.walk(tree) if isinstance(node, ast.While)]

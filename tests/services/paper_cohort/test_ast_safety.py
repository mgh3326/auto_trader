from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "app" / "services" / "paper_cohort"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_canonical_capture_has_only_unsigned_public_binance_boundary() -> None:
    imports = _imports(PACKAGE / "market_snapshot.py")
    assert "app.services.brokers.binance.rest_client" in imports
    assert "app.services.brokers.binance.dto" in imports
    forbidden = (
        "demo_scalping",
        "snapshot_bundle",
        "rob838",
        "signed",
        "execution_client",
        "mcp_server",
        "live",
    )
    assert not any(
        fragment in module.lower() for module in imports for fragment in forbidden
    )


def test_runner_uses_only_rob845_application_submit_boundary() -> None:
    source = (PACKAGE / "runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _imports(PACKAGE / "runner.py")
    assert "app.services.brokers.paper.composition" in imports
    assert "build_paper_execution_application" in source
    forbidden_imports = (
        "mcp_server",
        "execution_client",
        "submit_service",
        "demo_scalping_exec",
        "alpaca_paper_order_application",
    )
    assert not any(
        fragment in module for module in imports for fragment in forbidden_imports
    )
    submit_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
    ]
    assert len(submit_calls) == 1


def test_domain_does_not_redeclare_rob845_profiles_ports_or_capabilities() -> None:
    forbidden_defs = {
        "PaperExecutionApplication",
        "PaperBrokerCapabilities",
        "PaperBrokerPort",
        "PaperAdapterRegistry",
        "PaperOrderRequest",
    }
    found: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in forbidden_defs
        )
    assert found == set()


def test_task_decorator_is_not_declared_in_job_layer() -> None:
    source = (REPO_ROOT / "app" / "jobs" / "paper_cohort.py").read_text(
        encoding="utf-8"
    )
    assert "@broker.task" not in source
    assert "@taskiq_broker.task" not in source
